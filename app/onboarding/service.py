"""Evidence-based setup reviews. A checkbox never provisions or activates a channel."""

import hashlib
import json
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.auth.human_models import HumanAccount
from app.auth.policies import Permission, has_permission
from app.billing.service import billing_view
from app.configuration.models import ConfigurationDraft
from app.configuration.schemas import OperationalConfiguration
from app.configuration.service import ConfigurationService
from app.errors import ApplicationError, AuthorizationError
from app.integrations.models import TenantIntegration
from app.knowledge.models import Document, DocumentVersion
from app.locations.models import Location
from app.onboarding.models import SetupProgress
from app.onboarding.schemas import (
    SetupCheck,
    SetupCommand,
    SetupLink,
    SetupStep,
    SetupStepView,
    SetupView,
)
from app.rbac.models import RolePermission, TenantRole
from app.rbac.service import RevisionConflictError, audit
from app.tenants.models import Tenant
from app.users.models import User


class SetupBlockedError(ApplicationError):
    status_code = 409
    code = "setup_blocked"
    public_message = "Complete the blocking checks before marking this step reviewed."


DEFINITIONS: list[
    tuple[SetupStep, str, tuple[Permission, ...], tuple[tuple[str, str, Permission], ...]]
] = [
    (
        "business",
        "Business, locations and hours",
        (Permission.TENANT_READ,),
        (
            ("Business identity", "settings/operations/business", Permission.TENANT_READ),
            ("Hours and holidays", "settings/operations/hours", Permission.TENANT_READ),
            ("Services and staff", "settings/business", Permission.BUSINESS_READ),
        ),
    ),
    (
        "administrator",
        "Accepted administrator",
        (Permission.USER_READ, Permission.ROLE_READ),
        (("Administrator membership", "settings/team", Permission.USER_READ),),
    ),
    (
        "team",
        "Roles and optional team invitations",
        (Permission.USER_READ, Permission.ROLE_READ),
        (
            ("Role definitions", "settings/roles", Permission.ROLE_READ),
            ("Team invitations", "settings/team", Permission.USER_READ),
        ),
    ),
    (
        "agent",
        "Agent, languages, services and routing",
        (Permission.AGENT_READ,),
        (
            ("Agent behavior", "settings/operations/agent", Permission.AGENT_READ),
            ("Languages", "settings/operations/languages", Permission.AGENT_READ),
            ("Services and resources", "settings/business", Permission.BUSINESS_READ),
        ),
    ),
    (
        "knowledge",
        "Approved knowledge",
        (Permission.KNOWLEDGE_READ,),
        (("Knowledge sources", "knowledge", Permission.KNOWLEDGE_READ),),
    ),
    (
        "channels",
        "Channels and integrations",
        (Permission.CHANNEL_READ, Permission.INTEGRATION_READ, Permission.SCHEDULING_READ),
        (
            ("Voice routing", "settings/operations/voice", Permission.CHANNEL_READ),
            ("Web chat", "settings/operations/chat", Permission.CHANNEL_READ),
            ("SMS and email", "settings/operations/messaging", Permission.CHANNEL_READ),
            ("Connections", "integrations", Permission.INTEGRATION_READ),
        ),
    ),
    (
        "billing",
        "Subscription and budget",
        (Permission.BILLING_READ,),
        (
            ("Subscription and funds", "billing", Permission.BILLING_READ),
            ("Budget preferences", "settings/operations/budget", Permission.BILLING_READ),
        ),
    ),
    (
        "tests",
        "Tests and readiness",
        (Permission.CONFIGURATION_READ,),
        (
            (
                "Configuration preview and validation",
                "settings/configuration",
                Permission.CONFIGURATION_READ,
            ),
        ),
    ),
    (
        "publication",
        "Publish and activate",
        (Permission.CONFIGURATION_READ,),
        (
            ("Review and publish", "settings/configuration", Permission.CONFIGURATION_READ),
            ("Activation readiness", "integrations", Permission.INTEGRATION_READ),
        ),
    ),
]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def may_read(context: RequestContext, permissions: tuple[Permission, ...]) -> bool:
    return all(
        has_permission(context, permission)
        or (
            permission in {Permission.AGENT_READ, Permission.CHANNEL_READ}
            and has_permission(context, Permission.CONFIGURATION_READ)
        )
        for permission in permissions
    )


class SetupService(ConfigurationService):
    async def _view_setup(
        self,
        session: AsyncSession,
        context: RequestContext,
        tenant: Tenant,
        draft: ConfigurationDraft,
        progress: SetupProgress | None,
    ) -> SetupView:
        document = OperationalConfiguration.model_validate(draft.document)
        locations = (
            await session.scalars(
                select(Location).where(Location.tenant_id == tenant.id).order_by(Location.id)
            )
        ).all()
        members = (
            await session.scalars(select(User).where(User.tenant_id == tenant.id).order_by(User.id))
        ).all()
        accounts = set(
            (
                await session.scalars(
                    select(HumanAccount.user_id).where(HumanAccount.tenant_id == tenant.id)
                )
            ).all()
        )
        roles = (
            await session.scalars(
                select(TenantRole).where(TenantRole.tenant_id == tenant.id).order_by(TenantRole.id)
            )
        ).all()
        grants = (
            await session.scalars(
                select(RolePermission)
                .where(RolePermission.tenant_id == tenant.id)
                .order_by(RolePermission.role_id, RolePermission.key)
            )
        ).all()
        custodians = {
            role.id
            for role in roles
            if all(
                any(
                    grant.role_id == role.id and grant.key == key and grant.allowed
                    for grant in grants
                )
                for key in (Permission.USER_MANAGE, Permission.ROLE_UPDATE)
            )
        }
        accepted = [
            member.id
            for member in members
            if member.is_active and member.id in accounts and member.role_id in custodians
        ]
        knowledge = (
            await session.execute(
                select(
                    Document.id, Document.active_version, DocumentVersion.id, DocumentVersion.status
                )
                .join(
                    DocumentVersion,
                    (DocumentVersion.document_id == Document.id)
                    & (DocumentVersion.version == Document.active_version),
                )
                .where(Document.tenant_id == tenant.id, Document.status == "approved")
                .order_by(Document.id)
            )
        ).all()
        approved = [row for row in knowledge if row.status == "ready"]
        connections = (
            await session.scalars(
                select(TenantIntegration)
                .where(TenantIntegration.tenant_id == tenant.id)
                .order_by(TenantIntegration.id)
            )
        ).all()
        validation = await self._validate(session, draft)
        configurations = document.model_dump(mode="json")
        checks: dict[str, list[SetupCheck]] = {}
        snapshots: dict[str, Any] = {}

        def check(step: str, key: str, ready: bool, message: str) -> None:
            checks.setdefault(step, []).append(SetupCheck(key=key, ready=ready, message=message))

        check(
            "business",
            "identity",
            bool(document.business.name),
            "Set the business name and time zone.",
        )
        check("business", "locations", bool(locations), "Create at least one business location.")
        check(
            "business",
            "hours",
            bool(document.hours.windows),
            "Configure weekly opening hours; an empty schedule leaves the business closed.",
        )
        check(
            "administrator",
            "accepted_custodian",
            bool(accepted),
            "An active administrator must accept membership and be able to manage users and roles.",
        )
        check(
            "team",
            "custodian_roles",
            bool(custodians),
            "Keep a role capable of managing users and roles. Team invitations are optional.",
        )
        check(
            "agent",
            "behavior",
            bool(document.agent.greeting and document.languages.enabled_languages),
            "Review agent behavior, permitted actions and enabled languages.",
        )
        check(
            "knowledge",
            "approved_sources",
            not (
                "get_knowledge" in document.agent.allowed_tools
                or any(
                    "get_knowledge" in (scope.allowed_tools or [])
                    for scope in document.scopes.agents.values()
                )
            )
            or bool(approved),
            "Approve a ready knowledge source before enabling knowledge answers, "
            "or remove that action.",
        )
        channel_issues = [
            issue
            for issue in validation.issues
            if issue.field.split(".")[0] in {"voice", "chat", "messaging", "scheduling"}
            or issue.field.startswith("scopes.channels")
        ]
        check(
            "channels",
            "enabled_channel_readiness",
            not channel_issues,
            "Every enabled channel must pass connection, capability and dispatch checks. "
            "Disabled channels remain disabled.",
        )
        funding = await billing_view(session, tenant.id)
        check(
            "billing",
            "funding",
            funding.funding_ready,
            "A funded subscription and usable balance are required before activation.",
        )
        check(
            "tests",
            "validation",
            validation.valid,
            "Validate the saved configuration and resolve its readiness issues.",
        )
        check(
            "tests",
            "runtime_verification",
            not any(
                (
                    document.voice.enabled,
                    document.chat.enabled,
                    document.messaging.sms_enabled,
                    document.messaging.email_enabled,
                    document.scheduling.enabled,
                )
            )
            and not any(
                scope.chat is not None and scope.chat.enabled
                for scope in document.scopes.channels.values()
            ),
            "No live channel tests are required while all channels are disabled. "
            "Verified test results are required before enabling a channel.",
        )
        check(
            "publication",
            "published_revision",
            draft.published_version is not None,
            "Publish a validated configuration revision.",
        )
        check(
            "publication",
            "activation",
            tenant.service_state == "live",
            "Activation requires an accepted administrator, funding and ready channels. "
            "Completing this guide does not activate service.",
        )
        snapshots["business"] = {
            "business": configurations["business"],
            "hours": configurations["hours"],
            "locations": [(row.id, row.name, row.timezone) for row in locations],
            "location_overrides": configurations["scopes"]["locations"],
        }
        snapshots["administrator"] = {
            "accepted": accepted,
            "custodians": sorted(str(identity) for identity in custodians),
        }
        snapshots["team"] = {
            "roles": [(role.id, role.revision) for role in roles],
            "members": [
                (member.id, member.role_id, member.is_active, member.id in accounts)
                for member in members
            ],
        }
        snapshots["agent"] = {
            "agent": configurations["agent"],
            "languages": configurations["languages"],
            "agents": configurations["scopes"]["agents"],
        }
        snapshots["knowledge"] = [
            (row.id, row.active_version, row[2], row.status) for row in approved
        ]
        snapshots["channels"] = {
            "sections": {
                key: configurations[key] for key in ("voice", "chat", "messaging", "scheduling")
            },
            "connections": [(row.id, row.status, row.active_version_id) for row in connections],
            "channels": configurations["scopes"]["channels"],
        }
        snapshots["billing"] = {
            "budget": configurations["budget"],
            "funding": {
                "ready": funding.funding_ready,
                "subscription": funding.subscription.model_dump(mode="json")
                if funding.subscription
                else None,
            },
        }
        snapshots["tests"] = {
            "revision": draft.revision,
            "validation": validation.model_dump(mode="json"),
            "runtime_evidence": "unavailable",
        }
        snapshots["publication"] = {
            "published_version": draft.published_version,
            "state": tenant.service_state,
        }
        reviewed = progress.reviewed if progress else {}
        steps: list[SetupStepView] = []
        for key, title, permissions, links in DEFINITIONS:
            fingerprint = digest(
                {
                    "snapshot": snapshots[key],
                    "checks": [(item.key, item.ready) for item in checks[key]],
                }
            )
            readable = may_read(context, permissions)
            status = (
                "restricted"
                if not readable
                else "blocked"
                if any(not item.ready for item in checks[key])
                else "reviewed"
                if reviewed.get(key) == fingerprint
                else "needs_review"
            )
            steps.append(
                SetupStepView(
                    key=key,
                    title=title,
                    status=status,
                    fingerprint=fingerprint if readable else None,
                    checks=checks[key] if readable else [],
                    links=[
                        SetupLink(label=label, path=path, permission=permission)
                        for label, path, permission in links
                        if may_read(context, (permission,))
                    ],
                    review_permissions=list(permissions),
                )
            )
        return SetupView(
            tenant_id=tenant.id,
            revision=progress.revision if progress else 0,
            configuration_revision=draft.revision,
            published_version=draft.published_version,
            current_step=cast(SetupStep, progress.current_step) if progress else "business",
            service_state=cast(Literal["setup", "live"], tenant.service_state),
            steps=steps,
        )

    async def get_setup(self, context: RequestContext) -> SetupView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.TENANT_READ, lock=True)
            draft = await self._draft(session, tenant)
            progress = await session.scalar(
                select(SetupProgress).where(SetupProgress.tenant_id == tenant.id)
            )
            return await self._view_setup(session, context, tenant, draft, progress)

    async def save_setup(self, context: RequestContext, command: SetupCommand) -> SetupView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.TENANT_UPDATE, lock=True)
            draft = await self._draft(session, tenant)
            progress = await session.scalar(
                select(SetupProgress).where(SetupProgress.tenant_id == tenant.id)
            )
            if (
                command.revision != (progress.revision if progress else 0)
                or command.configuration_revision != draft.revision
            ):
                raise RevisionConflictError()
            view = await self._view_setup(session, context, tenant, draft, progress)
            step = next(item for item in view.steps if item.key == command.step)
            if step.fingerprint is None:
                raise AuthorizationError()
            if step.fingerprint != command.expected_fingerprint:
                raise RevisionConflictError()
            if command.action == "review" and step.status == "blocked":
                raise SetupBlockedError()
            if progress is None:
                progress = SetupProgress(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    current_step=command.step,
                    revision=1,
                    reviewed={},
                )
                session.add(progress)
            else:
                progress.revision += 1
                progress.current_step = command.step
            if command.action == "review":
                progress.reviewed = {**progress.reviewed, command.step: step.fingerprint}
            audit(
                session,
                context,
                "onboarding_step_reviewed"
                if command.action == "review"
                else "onboarding_step_selected",
                "onboarding",
                progress.id,
            )
            await session.flush()
            return await self._view_setup(session, context, tenant, draft, progress)
