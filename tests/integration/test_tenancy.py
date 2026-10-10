import asyncio
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.audit.events import AuditAction
from app.audit.models import AuditEvent
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission
from app.core.database.rls import UnsafeDatabaseRoleError, set_tenant_scope
from app.core.database.session import Database
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.settings import Settings
from app.identity.models import Account, AccountSession, AuthChallenge
from app.identity.service import IdentityService
from app.identity.sessions import WorkspaceRequiredError, authenticate_workspace
from app.main import create_app
from app.tenants.models import Tenant
from app.users.models import User
from tests.fakes import FakeDependency
from tests.integration.workspaces import Workspace, create_workspace

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "true"
        and os.environ.get("RUN_POSTGRES_INTEGRATION") != "true",
        reason="requires disposable PostgreSQL with separate migration/runtime credentials",
    ),
]


@dataclass
class TenancyFixture:
    settings: Settings
    database: Database
    admin: async_sessionmaker[AsyncSession]
    a: Workspace
    b: Workspace

    def context(self, tenant: Workspace) -> RequestContext:
        return RequestContext(
            uuid4(),
            tenant.tenant_id,
            tenant.user_id,
            ActorType.USER,
            role_id=uuid5(tenant.tenant_id, "role:owner"),
            authorization_revision=1,
            capabilities=frozenset(item.value for item in Permission),
        )


@pytest.fixture
async def tenancy() -> AsyncGenerator[TenancyFixture]:
    if (
        os.environ.get("RUN_INTEGRATION") != "true"
        and os.environ.get("RUN_POSTGRES_INTEGRATION") != "true"
    ):
        pytest.skip("requires explicit opt-in to disposable PostgreSQL")
    # Provider keys, mail and public addresses are blanked in tests/conftest.py, so
    # nothing here comes from a developer's local .env.
    settings = Settings.from_environment(
        db_pool_size=1,
        db_max_overflow=0,
    )
    assert settings.migration_database_url is not None, "Explicit admin test credentials required"
    engine = create_async_engine(settings.migration_url, poolclass=NullPool, hide_parameters=True)
    admin = async_sessionmaker(engine, expire_on_commit=False)
    database = Database(settings)
    try:
        # One person owns both workspaces: the same account with two memberships.
        # A new account for every test, so one test cannot disable the next one's owner.
        owner = f"owner-{uuid4().hex}@example.test"
        a = await create_workspace(admin, name="Tenant A", email=owner, display_name="Admin A")
        b = await create_workspace(admin, name="Tenant B", email=owner, display_name="Admin B")
        yield TenancyFixture(settings, database, admin, a, b)
    finally:
        await database.close()
        await engine.dispose()


async def test_rls_blocks_cross_tenant_reads_without_repository_filters(
    tenancy: TenancyFixture,
) -> None:
    for tenant in (tenancy.a, tenancy.b):
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, tenant.tenant_id)
            # Acceptance gate: deliberately NO WHERE clauses on any tenant table.
            assert (await session.scalars(select(Tenant.id))).all() == [tenant.tenant_id]
            assert (await session.scalars(select(User.id))).all() == [tenant.user_id]
            events = (await session.scalars(select(AuditEvent))).all()
            assert len(events) == 2
            assert all(event.tenant_id == tenant.tenant_id for event in events)


async def test_missing_context_and_pooled_connections_fail_closed(tenancy: TenancyFixture) -> None:
    async with tenancy.database.session_factory.begin() as session:
        assert (await session.scalars(select(User))).all() == []
        backend = await session.scalar(text("SELECT pg_backend_pid()"))
        await set_tenant_scope(session, tenancy.a.tenant_id)
        assert (await session.scalars(select(User.id))).all() == [tenancy.a.user_id]
    async with tenancy.database.session_factory.begin() as session:
        assert await session.scalar(text("SELECT pg_backend_pid()")) == backend
        assert (await session.scalars(select(User))).all() == []
        assert (
            await session.scalar(text("SELECT nullif(current_setting('app.tenant_id', true), '')"))
            is None
        )
    with pytest.raises(RuntimeError, match="rollback"):
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, tenancy.b.tenant_id)
            raise RuntimeError("rollback")
    async with tenancy.database.session_factory.begin() as session:
        assert (await session.scalars(select(User))).all() == []


async def test_rls_rejects_cross_tenant_and_unscoped_inserts(tenancy: TenancyFixture) -> None:
    for scope in (tenancy.a.tenant_id, None):
        with pytest.raises(DBAPIError):
            async with tenancy.database.session_factory.begin() as session:
                if scope is not None:
                    await set_tenant_scope(session, scope)
                await session.execute(
                    insert(AuditEvent).values(
                        id=uuid4(),
                        tenant_id=tenancy.b.tenant_id,
                        actor_type="system",
                        action="forbidden",
                        resource_type="tenant",
                        request_id=uuid4(),
                    )
                )


async def test_composite_foreign_key_rejects_cross_tenant_user(tenancy: TenancyFixture) -> None:
    with pytest.raises(IntegrityError):
        async with tenancy.admin.begin() as session:
            session.add(
                AuthChallenge(
                    account_id=tenancy.a.account_id,
                    tenant_id=tenancy.a.tenant_id,
                    user_id=tenancy.b.user_id,
                    purpose="invitation",
                    key_digest="1" * 64,
                    token_digest="0" * 64,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )


async def test_superuser_and_owner_connections_are_rejected(tenancy: TenancyFixture) -> None:
    database = Database(
        tenancy.settings.model_copy(
            update={"database_url": SecretStr(tenancy.settings.migration_url)}
        )
    )
    try:
        with pytest.raises(UnsafeDatabaseRoleError):
            await database.ping()
    finally:
        await database.close()


async def test_uow_commit_rollback_and_audit_immutability(tenancy: TenancyFixture) -> None:
    context = tenancy.context(tenancy.a)
    uow = UnitOfWork(tenancy.database.session_factory)
    async with uow.transaction(context) as tx:
        AuditService(tx.audit).record(context, AuditAction.USERS_VIEWED, "users")
    with pytest.raises(RuntimeError, match="rollback"):
        async with uow.transaction(context) as tx:
            AuditService(tx.audit).record(context, AuditAction.AUDIT_VIEWED, "audit_events")
            raise RuntimeError("rollback")
    async with uow.transaction(context) as tx:
        events = await tx.audit.list(100, 0)
        assert len(events) == 3
        assert any(
            e.request_id == context.request_id and e.actor_id == context.actor_id for e in events
        )
    # Even the migration owner cannot mutate audit rows through ordinary SQL.
    for sql in (
        "UPDATE audit_events SET action = 'changed' WHERE tenant_id = :tenant",
        "DELETE FROM audit_events WHERE tenant_id = :tenant",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(DBAPIError):
            async with tenancy.admin.begin() as session:
                await session.execute(text(sql), {"tenant": tenancy.a.tenant_id})


async def test_concurrent_contexts_are_independent(tenancy: TenancyFixture) -> None:
    uow = UnitOfWork(tenancy.database.session_factory)

    async def read(tenant: Workspace) -> list[UUID]:
        async with uow.transaction(tenancy.context(tenant)) as tx:
            return [user.id for user in await tx.users.list(100, 0)]

    results = await asyncio.gather(read(tenancy.a), read(tenancy.b), read(tenancy.a))
    assert results == [[tenancy.a.user_id], [tenancy.b.user_id], [tenancy.a.user_id]]


async def test_session_forgery_and_sign_out(tenancy: TenancyFixture) -> None:
    sessions = tenancy.database.session_factory
    token = tenancy.a.token.get_secret_value()
    context = await authenticate_workspace(sessions, token, uuid4())
    assert (context.tenant_id, context.actor_id) == (tenancy.a.tenant_id, tenancy.a.user_id)
    assert context.session_id == tenancy.a.session_id
    other = tenancy.b.token.get_secret_value()
    for forged in (
        # Another session's identifier with this session's secret.
        token.replace(str(tenancy.a.session_id), str(tenancy.b.session_id)),
        token + "x",
        token[:-1] + ("A" if token[-1] != "A" else "B"),
        other.rsplit(".", 1)[0] + "." + token.rsplit(".", 1)[1],
    ):
        with pytest.raises(AuthenticationError):
            await authenticate_workspace(sessions, forged, uuid4())
    identity = IdentityService(sessions, tenancy.settings)
    await identity.logout(token, uuid4())
    await identity.logout(token, uuid4())  # Signing out twice is harmless.
    with pytest.raises(AuthenticationError):
        await authenticate_workspace(sessions, token, uuid4())
    # The same person's other session, in the other workspace, is untouched.
    assert (await authenticate_workspace(sessions, other, uuid4())).tenant_id == tenancy.b.tenant_id
    # A context built before the sign-out is refused inside its next transaction.
    with pytest.raises(AuthenticationError):
        async with UnitOfWork(sessions).transaction(context):
            pass


@pytest.mark.parametrize("disabled", ["user", "tenant", "expired", "account", "password"])
async def test_inactive_or_expired_identity_denied(tenancy: TenancyFixture, disabled: str) -> None:
    async with tenancy.admin.begin() as session:
        if disabled == "user":
            await session.execute(
                update(User).where(User.id == tenancy.a.user_id).values(is_active=False)
            )
        elif disabled == "tenant":
            await session.execute(
                update(Tenant).where(Tenant.id == tenancy.a.tenant_id).values(is_active=False)
            )
        elif disabled == "expired":
            await session.execute(
                update(AccountSession)
                .where(AccountSession.id == tenancy.a.session_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif disabled == "account":
            await session.execute(
                update(Account).where(Account.id == tenancy.a.account_id).values(is_active=False)
            )
        else:
            # A password change makes every earlier session stale.
            await session.execute(
                update(Account)
                .where(Account.id == tenancy.a.account_id)
                .values(credential_revision=Account.credential_revision + 1)
            )
    # A lost membership or closed workspace leaves the sign-in valid but with no workspace;
    # anything wrong with the session or the account itself is an authentication failure.
    expected = WorkspaceRequiredError if disabled in {"user", "tenant"} else AuthenticationError
    with pytest.raises(expected):
        await authenticate_workspace(
            tenancy.database.session_factory, tenancy.a.token.get_secret_value(), uuid4()
        )


async def test_api_tenant_resolution_permissions_and_audit(tenancy: TenancyFixture) -> None:
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
    request_id = str(uuid4())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        response = await client.get("/api/v1/tenants/current")
        assert response.status_code == 200
        assert response.json()["id"] == str(tenancy.a.tenant_id)
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/api/v1/users/me")).json()["id"] == str(tenancy.a.user_id)
        response = await client.get("/api/v1/users", headers={"X-Request-ID": request_id})
        assert response.status_code == 200
        assert [user["id"] for user in response.json()["items"]] == [str(tenancy.a.user_id)]
        assert "token_digest" not in response.text
        assert (await client.get("/api/v1/users?limit=101")).status_code == 422
        # The workspace comes from the session; a workspace named by the browser is ignored.
        named = await client.get("/api/v1/users", headers={"X-Tenant-ID": str(tenancy.b.tenant_id)})
        assert [user["id"] for user in named.json()["items"]] == [str(tenancy.a.user_id)]
        audits = (await client.get("/api/v1/audit-events")).json()["items"]
        assert any(event["request_id"] == request_id for event in audits)
        async with tenancy.admin.begin() as session:
            await session.execute(
                update(User)
                .where(User.id == tenancy.a.user_id)
                .values(role_id=uuid5(tenancy.a.tenant_id, "role:member"))
            )
        assert (await client.get("/api/v1/tenants/current")).status_code == 200
        assert (await client.get("/api/v1/users")).status_code == 403
        assert (await client.get("/api/v1/audit-events")).status_code == 403
    with pytest.raises(AuthorizationError):
        async with UnitOfWork(tenancy.database.session_factory).transaction(
            tenancy.context(tenancy.a)
        ):
            pass
