"""Password-proven workspace selection. A chooser token cannot authorize tenant APIs."""

import asyncio
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.human import issue_membership_session
from app.auth.human_models import HumanAccount, HumanIdentity
from app.auth.human_schemas import HumanSessionView
from app.auth.identity import identity_for_email, identity_scope
from app.auth.tokens import digest_token
from app.auth.workspace_models import IdentityMembership, IdentitySession
from app.auth.workspace_schemas import (
    IdentityCredential,
    IdentityLogin,
    IdentitySessionView,
    WorkspaceOption,
    WorkspaceSelection,
    WorkspacesView,
)
from app.config.settings import Settings
from app.database.rls import set_tenant_scope, verify_runtime_role
from app.errors import AuthenticationError, AuthorizationError
from app.platform.passwords import DUMMY_HASH, verify_password
from app.platform.throttle import AuthenticationUnavailableError
from app.rbac.models import TenantRole
from app.tenants.models import Tenant
from app.users.models import User


class WorkspaceIdentityService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    def enabled(self) -> None:
        if not self.settings.tenant_human_auth_enabled:
            raise AuthenticationUnavailableError()

    async def validate(
        self, session: AsyncSession, credential: IdentityCredential, *, lock: bool = False
    ) -> tuple[IdentitySession, HumanIdentity]:
        self.enabled()
        token = credential.token.get_secret_value()
        try:
            prefix, identifier, secret = token.split(".")
            if prefix != "fgs1" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
                raise ValueError()
            identity = UUID(identifier)
        except ValueError:
            raise AuthenticationError() from None
        await verify_runtime_role(session)
        await identity_scope(session, credential.email)
        row = await session.scalar(
            select(IdentitySession)
            .where(IdentitySession.id == identity)
            .with_for_update(read=not lock)
        )
        owner = await identity_for_email(session, credential.email, lock=True)
        if (
            row is None
            or owner is None
            or row.identity_id != owner.id
            or row.revoked_at is not None
            or row.expires_at <= datetime.now(UTC)
            or row.credential_revision != owner.credential_revision
            or owner.password_hash is None
            or not hmac.compare_digest(row.token_digest, digest_token(token))
        ):
            raise AuthenticationError()
        return row, owner

    async def login(self, command: IdentityLogin, request_id: UUID) -> IdentitySessionView:
        self.enabled()
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            owner = await identity_for_email(session, command.email)
            previous_hash = owner.password_hash if owner else None
            previous_revision = owner.credential_revision if owner else None
        valid = await asyncio.to_thread(
            verify_password, command.password.get_secret_value(), previous_hash or DUMMY_HASH
        )
        if not valid or previous_hash is None:
            raise AuthenticationError()
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            owner = await identity_for_email(session, command.email, lock=True)
            if (
                owner is None
                or owner.password_hash != previous_hash
                or owner.credential_revision != previous_revision
            ):
                raise AuthenticationError()
            identity = uuid4()
            token = f"fgs1.{identity}.{secrets.token_urlsafe(32)}"
            expires = datetime.now(UTC) + timedelta(hours=1)
            session.add(
                IdentitySession(
                    id=identity,
                    identity_id=owner.id,
                    credential_revision=owner.credential_revision,
                    token_digest=digest_token(token),
                    expires_at=expires,
                    request_id=request_id,
                )
            )
            return IdentitySessionView(access_token=token, email=owner.email, expires_at=expires)

    async def workspaces(self, credential: IdentityCredential) -> WorkspacesView:
        async with self.sessions.begin() as session:
            admission, identity = await self.validate(session, credential)
            expires, identity_id = admission.expires_at, identity.id
            memberships = [
                (row.tenant_id, row.user_id)
                for row in await session.scalars(
                    select(IdentityMembership).where(IdentityMembership.identity_id == identity.id)
                )
            ]
        items: list[WorkspaceOption] = []
        # A separate transaction per workspace keeps forced RLS and the identity map isolated.
        for tenant_id, user_id in memberships:
            async with self.sessions.begin() as session:
                await set_tenant_scope(session, tenant_id)
                tenant, user = (
                    await session.get(Tenant, tenant_id),
                    await session.get(User, user_id),
                )
                account = await session.scalar(
                    select(HumanAccount.id).where(
                        HumanAccount.tenant_id == tenant_id,
                        HumanAccount.user_id == user_id,
                        HumanAccount.identity_id == identity_id,
                    )
                )
                candidates = await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.tenant_id == tenant_id, func.lower(User.email) == credential.email)
                )
                if (
                    tenant is None
                    or user is None
                    or account is None
                    or not tenant.is_active
                    or not user.is_active
                ):
                    continue
                if candidates != 1:
                    continue
                role = await session.get(TenantRole, user.role_id)
                if role is None:
                    continue
                items.append(
                    WorkspaceOption(
                        tenant_id=tenant.id,
                        business_name=tenant.name,
                        user_id=user.id,
                        display_name=user.display_name,
                        role_name=role.name,
                        service_state=tenant.service_state,
                    )
                )
        return WorkspacesView(
            items=sorted(items, key=lambda item: (item.business_name, item.tenant_id)),
            expires_at=expires,
        )

    async def select(self, credential: WorkspaceSelection, request_id: UUID) -> HumanSessionView:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, credential.tenant_id)
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == credential.tenant_id).with_for_update()
            )
            admission, owner = await self.validate(session, credential)
            membership = await session.scalar(
                select(HumanAccount).where(
                    HumanAccount.tenant_id == credential.tenant_id,
                    HumanAccount.identity_id == owner.id,
                )
            )
            candidates = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.tenant_id == credential.tenant_id, func.lower(User.email) == owner.email
                )
            )
            user = await session.get(User, membership.user_id) if membership is not None else None
            if (
                tenant is None
                or not tenant.is_active
                or user is None
                or not user.is_active
                or candidates != 1
            ):
                raise AuthorizationError()
            role = await session.get(TenantRole, user.role_id)
            if role is None:
                raise AuthorizationError()
            result = issue_membership_session(session, user, owner)
            session.add(
                AuditEvent(
                    tenant_id=tenant.id,
                    actor_id=user.id,
                    actor_type="user",
                    action="human_workspace_selected",
                    resource_type="identity_session",
                    resource_id=admission.id,
                    request_id=request_id,
                )
            )
            return result

    async def logout(self, credential: IdentityCredential) -> None:
        async with self.sessions.begin() as session:
            admission, _ = await self.validate(session, credential, lock=True)
            admission.revoked_at = datetime.now(UTC)
