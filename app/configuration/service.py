import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.catalog import ALL_TOOLS
from app.auth.context import RequestContext
from app.auth.policies import Permission, has_permission, require_permission
from app.configuration.defaults import DEFAULTS_VERSION, tenant_defaults
from app.configuration.diff import configuration_changes
from app.configuration.models import (
    ConfigurationDraft,
    ConfigurationEvent,
    ConfigurationResource,
    PublishedConfiguration,
)
from app.configuration.resolution import SCOPE_FIELDS, resolve_configuration
from app.configuration.schemas import (
    MAX_SCOPE_WRITE_BATCH,
    ConfigurationDiff,
    ConfigurationHead,
    ConfigurationScopesResult,
    ConfigurationScopeView,
    ConfigurationSectionView,
    ConfigurationTest,
    ConfigurationTestView,
    ConfigurationValidation,
    ConfigurationVersionView,
    ConfigurationView,
    EffectiveConfigurationView,
    OperationalConfiguration,
    SaveConfiguration,
    SaveConfigurationScopes,
    SaveConfigurationSection,
    ScopeChange,
    ValidationIssue,
)
from app.configuration.sections import (
    SCOPE_SECTION,
    SECTION_AUTHORITY,
    ConfigurationSection,
    InitialPublicationRequiredError,
    InvalidConfigurationSectionError,
    SectionPublicationConflictError,
    authority,
    in_publication_area,
)
from app.database.unit_of_work import enter_tenant_scope
from app.errors import ConflictError, NotFoundError
from app.integrations.models import TenantIntegration
from app.rbac.service import RevisionConflictError, audit
from app.tenants.models import Tenant

UNAVAILABLE_FEATURES = {
    "voice.enabled": "Voice activation awaits usage reservations and carrier verification.",
    "chat.enabled": "Web chat dispatch has not completed integration verification.",
    "scheduling.enabled": "Calendar booking adapters have not completed integration verification.",
    "messaging.sms_enabled": "Transactional SMS dispatch has not been verified.",
    "messaging.email_enabled": "Business email dispatch has not been verified.",
    "privacy.recording_enabled": "Carrier disclosure and recording consent are not verified.",
    "budget.auto_recharge": "Payment authorization and reconciliation are not verified.",
    **{
        f"workflows.{name}": "Durable dispatch and immediate revocation checks are not verified."
        for name in (
            "appointment_reminders",
            "missed_call_follow_up",
            "consented_callbacks",
            "lead_assignment",
            "ticket_escalation",
            "post_call_sync",
        )
    },
}


def is_business_open(document: OperationalConfiguration, instant: datetime) -> bool:
    if instant.tzinfo is None:
        raise ValueError("Use an aware instant")
    local = instant.astimezone(ZoneInfo(document.business.timezone))
    if local.date() in document.hours.holidays:
        return False
    return any(
        window.weekday == local.weekday() and window.opens <= local.time() < window.closes
        for window in document.hours.windows
    )


async def seed_configuration(session: AsyncSession, tenant: Tenant) -> ConfigurationDraft:
    row = await session.scalar(
        select(ConfigurationDraft).where(ConfigurationDraft.tenant_id == tenant.id)
    )
    if row is not None:
        return row
    document = tenant_defaults()
    document.business.name = tenant.name
    document.voice.transfer_number = tenant.human_transfer_number
    if tenant.language_policy:
        document.languages = document.languages.model_validate(tenant.language_policy)
    row = ConfigurationDraft(
        tenant_id=tenant.id,
        revision=1,
        defaults_version=DEFAULTS_VERSION,
        document=document.model_dump(mode="json"),
    )
    session.add(row)
    await session.flush()
    from app.configuration.hours import sync_hours

    await sync_hours(session, tenant.id, document)
    return row


class ConfigurationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def catalog(self, context: RequestContext) -> dict[str, object]:
        result = configuration_catalog()
        result["connections"] = []
        async with self.sessions.begin() as session:
            await self._enter(session, context, Permission.TENANT_READ)
            rows = (
                await session.scalars(
                    select(TenantIntegration)
                    .where(TenantIntegration.tenant_id == context.tenant_id)
                    .order_by(TenantIntegration.purpose, TenantIntegration.slot)
                )
            ).all()
            result["connections"] = [
                {
                    "id": str(row.id),
                    "provider": row.provider,
                    "purpose": row.purpose,
                    "slot": row.slot,
                    "status": row.status,
                }
                for row in rows
                if any(
                    has_permission(context, permission)
                    for permission in (
                        Permission.CONFIGURATION_READ,
                        Permission.INTEGRATION_READ,
                        Permission.CHANNEL_READ,
                        Permission.SCHEDULING_READ,
                    )
                )
            ]
            from app.locations.models import Location

            resources = (
                await session.scalars(
                    select(ConfigurationResource)
                    .where(ConfigurationResource.tenant_id == context.tenant_id)
                    .order_by(ConfigurationResource.kind, ConfigurationResource.name)
                )
            ).all()
            locations = (
                await session.scalars(
                    select(Location)
                    .where(Location.tenant_id == context.tenant_id)
                    .order_by(Location.name)
                )
            ).all()
            result["scope_resources"] = [
                {
                    "id": str(row.id),
                    "kind": row.kind,
                    "channel_kind": row.channel_kind,
                    "name": row.name,
                    "revision": row.revision,
                    "is_active": row.is_active,
                }
                for row in resources
                if has_permission(context, Permission.CONFIGURATION_READ)
                or (
                    row.kind == "agent"
                    and any(
                        has_permission(context, permission)
                        for permission in (Permission.AGENT_READ, Permission.CHANNEL_READ)
                    )
                )
                or (row.kind == "channel" and has_permission(context, Permission.CHANNEL_READ))
            ]
            result["locations"] = [
                {"id": str(row.id), "name": row.name, "timezone": row.timezone} for row in locations
            ]
            from app.calls.models import PhoneNumber

            phones = (
                await session.scalars(
                    select(PhoneNumber)
                    .where(PhoneNumber.tenant_id == context.tenant_id)
                    .order_by(PhoneNumber.number)
                )
            ).all()
            result["phone_numbers"] = [
                {"id": str(phone.id), "number": phone.number, "is_active": phone.is_active}
                for phone in phones
                if any(
                    has_permission(context, permission)
                    for permission in (
                        Permission.CONFIGURATION_READ,
                        Permission.INTEGRATION_READ,
                        Permission.CHANNEL_READ,
                    )
                )
            ]
        return result

    async def _enter(
        self,
        session: AsyncSession,
        context: RequestContext,
        permission: Permission,
        *,
        lock: bool = False,
    ) -> Tenant:
        require_permission(context, permission)
        from app.database.rls import set_tenant_scope

        await set_tenant_scope(session, context.tenant_id)
        query = select(Tenant).where(Tenant.id == context.tenant_id)
        tenant = await session.scalar(query.with_for_update() if lock else query)
        if tenant is None:
            raise NotFoundError()
        await enter_tenant_scope(session, context)
        return tenant

    async def _draft(self, session: AsyncSession, tenant: Tenant) -> ConfigurationDraft:
        return await seed_configuration(session, tenant)

    @staticmethod
    def _view(row: ConfigurationDraft) -> ConfigurationView:
        return ConfigurationView(
            revision=row.revision,
            defaults_version=row.defaults_version,
            published_version=row.published_version,
            document=OperationalConfiguration.model_validate(row.document),
        )

    async def get(self, context: RequestContext) -> ConfigurationView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.CONFIGURATION_READ, lock=True)
            return self._view(await self._draft(session, tenant))

    async def head(self, context: RequestContext) -> ConfigurationHead:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.TENANT_READ)
            row = await session.scalar(
                select(ConfigurationDraft).where(ConfigurationDraft.tenant_id == tenant.id)
            )
            return ConfigurationHead(
                tenant_id=tenant.id,
                draft_revision=row.revision if row else 0,
                published_version=row.published_version if row else None,
            )

    async def save(self, context: RequestContext, command: SaveConfiguration) -> ConfigurationView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.CONFIGURATION_UPDATE, lock=True)
            row = await self._draft(session, tenant)
            if row.revision != command.revision:
                raise RevisionConflictError()
            current = OperationalConfiguration.model_validate(row.document)
            if (
                "directory" in command.document.model_fields_set
                and command.document.directory != current.directory
            ):
                raise ConflictError()
            command.document.directory = current.directory
            await self._stage(session, context, row, command.document)
            return self._view(row)

    async def _stage(
        self,
        session: AsyncSession,
        context: RequestContext,
        row: ConfigurationDraft,
        document: OperationalConfiguration,
    ) -> None:
        if document.business.logo_object_key and not document.business.logo_object_key.startswith(
            f"tenants/{context.tenant_id}/branding/"
        ):
            raise ConflictError()
        row.document = document.model_dump(mode="json")
        from app.configuration.hours import sync_hours

        await sync_hours(session, context.tenant_id, document)
        row.revision += 1
        audit(session, context, "configuration_draft_saved", "configuration", row.id)
        await session.flush()

    async def _section_view(
        self, session: AsyncSession, row: ConfigurationDraft, section: ConfigurationSection
    ) -> ConfigurationSectionView:
        document = OperationalConfiguration.model_validate(row.document)
        published = await published_for_tenant(session, row.tenant_id)
        permissions = SECTION_AUTHORITY[section]
        published_revision = (
            await session.scalar(
                select(PublishedConfiguration.source_revision).where(
                    PublishedConfiguration.tenant_id == row.tenant_id,
                    PublishedConfiguration.version == row.published_version,
                )
            )
            if row.published_version is not None
            else None
        )
        return ConfigurationSectionView(
            section=section,
            revision=row.revision,
            published_version=row.published_version,
            published_revision=published_revision,
            value=cast(JsonValue, document.model_dump(mode="json")[section]),
            published_value=cast(JsonValue, published[1].model_dump(mode="json")[section])
            if published
            else None,
            read_permission=permissions.read,
            update_permission=permissions.update,
            publish_permission=permissions.publish,
        )

    async def get_section(
        self, context: RequestContext, section: ConfigurationSection
    ) -> ConfigurationSectionView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(
                session, context, authority(section, "read", context), lock=True
            )
            return await self._section_view(session, await self._draft(session, tenant), section)

    async def section_diff(
        self, context: RequestContext, section: ConfigurationSection, revision: int
    ) -> ConfigurationDiff:
        async with self.sessions.begin() as session:
            tenant = await self._enter(
                session, context, authority(section, "read", context), lock=True
            )
            row = await self._draft(session, tenant)
            if row.revision != revision:
                raise RevisionConflictError()
            published = await published_for_tenant(session, tenant.id)
            changes = configuration_changes(
                published[1] if published else None,
                OperationalConfiguration.model_validate(row.document),
            )
            validation = await self._validate(session, row)
            visible_issues = [
                issue for issue in validation.issues if in_publication_area(section, issue.field)
            ]
            if published is None:
                visible_issues.append(
                    ValidationIssue(
                        field=section,
                        code="initial_publication_required",
                        message=InitialPublicationRequiredError.public_message,
                    )
                )
            elif any(not in_publication_area(section, change.field) for change in changes):
                visible_issues.append(
                    ValidationIssue(
                        field=section,
                        code="section_publication_conflict",
                        message=SectionPublicationConflictError.public_message,
                    )
                )
            if not validation.valid and not visible_issues:
                visible_issues.append(
                    ValidationIssue(
                        field=section,
                        code="workspace_not_ready",
                        message="Other workspace readiness checks have not passed. "
                        "Ask an administrator to review them.",
                    )
                )
            return ConfigurationDiff(
                revision=revision,
                published_version=row.published_version,
                changes=[
                    change for change in changes if in_publication_area(section, change.field)
                ],
                validation=ConfigurationValidation(
                    revision=revision,
                    valid=validation.valid and not visible_issues,
                    issues=visible_issues,
                ),
                impact=[
                    "Publication applies the saved changes in this configuration area atomically.",
                    "New conversations use the publication; active conversations retain "
                    "their pinned behavior.",
                    "Unrelated draft changes require an authorized review "
                    "of the complete configuration.",
                ],
            )

    async def save_section(
        self,
        context: RequestContext,
        section: ConfigurationSection,
        command: SaveConfigurationSection,
    ) -> ConfigurationSectionView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(
                session, context, authority(section, "update", context), lock=True
            )
            row = await self._draft(session, tenant)
            if row.revision != command.revision:
                raise RevisionConflictError()
            data = dict(row.document)
            data[section] = command.value
            try:
                document = OperationalConfiguration.model_validate(data)
            except ValidationError:
                raise InvalidConfigurationSectionError() from None
            await self._stage(session, context, row, document)
            return await self._section_view(session, row, section)

    async def save_scopes(
        self, context: RequestContext, command: SaveConfigurationScopes
    ) -> ConfigurationScopesResult:
        from app.calls.models import PhoneNumber
        from app.locations.models import Location

        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.TENANT_READ, lock=True)
            row = await self._draft(session, tenant)
            if row.revision != command.revision:
                raise RevisionConflictError()
            data = OperationalConfiguration.model_validate(row.document).model_dump(mode="json")
            for change in command.changes:
                section = SCOPE_SECTION[change.kind]
                authority(section, "update", context)
                resource = await session.get(
                    Location if change.kind == "location" else ConfigurationResource, change.id
                )
                if resource is None or (
                    isinstance(resource, ConfigurationResource) and resource.kind != change.kind
                ):
                    raise NotFoundError()
                entries = data["scopes"][change.kind + "s"]
                if change.value is None:
                    entries.pop(str(change.id), None)
                else:
                    entries[str(change.id)] = change.value
            try:
                document = OperationalConfiguration.model_validate(data)
            except ValidationError:
                raise InvalidConfigurationSectionError() from None
            for change in command.changes:
                scope = getattr(document.scopes, change.kind + "s").get(change.id)
                if scope is None:
                    continue
                location_id: UUID | None = getattr(scope, "location_id", None)
                if location_id and await session.get(Location, location_id) is None:
                    raise NotFoundError()
                if change.kind == "channel":
                    channel = document.scopes.channels[change.id]
                    resource = await session.get(ConfigurationResource, change.id)
                    if resource is None or resource.channel_kind != channel.kind:
                        raise ConflictError()
                    if channel.agent_id:
                        agent = await session.get(ConfigurationResource, channel.agent_id)
                        if agent is None or agent.kind != "agent":
                            raise NotFoundError()
                    if (
                        channel.phone_number_id
                        and await session.get(PhoneNumber, channel.phone_number_id) is None
                    ):
                        raise NotFoundError()
            await self._stage(session, context, row, document)
            canonical = document.scopes.model_dump(mode="json")
            return ConfigurationScopesResult(
                revision=row.revision,
                changes=[
                    ScopeChange(
                        kind=change.kind,
                        id=change.id,
                        value=cast(JsonValue, canonical[change.kind + "s"].get(str(change.id))),
                    )
                    for change in command.changes
                ],
            )

    async def get_scope(
        self, context: RequestContext, kind: str, identity: UUID
    ) -> ConfigurationScopeView:
        from app.locations.models import Location

        section = SCOPE_SECTION.get(kind)
        if section is None:
            raise NotFoundError()
        async with self.sessions.begin() as session:
            tenant = await self._enter(
                session, context, authority(section, "read", context), lock=True
            )
            resource = await session.get(
                Location if kind == "location" else ConfigurationResource, identity
            )
            if resource is None or (
                isinstance(resource, ConfigurationResource) and resource.kind != kind
            ):
                raise NotFoundError()
            row = await self._draft(session, tenant)
            view = await self._section_view(session, row, section)
            document = OperationalConfiguration.model_validate(row.document)
            own = document.scopes.model_dump(mode="json")[kind + "s"].get(str(identity))
            if own is None:
                own = (
                    {"kind": resource.channel_kind}
                    if isinstance(resource, ConfigurationResource) and kind == "channel"
                    else {}
                )
            published = await published_for_tenant(session, tenant.id)
            bound: dict[str, UUID | None] = {
                "location_id": None,
                "agent_id": None,
                "channel_id": None,
            }
            bound[kind + "_id"] = identity
            parent = document.model_copy(deep=True)
            parent_scopes = getattr(parent.scopes, kind + "s")
            parent_scope = cast(BaseModel | None, parent_scopes.get(identity))
            if parent_scope is not None:
                parent_scopes[identity] = parent_scope.model_copy(
                    update={
                        field: None
                        for field in type(parent_scope).model_fields
                        if field not in {"kind", "location_id", "agent_id", "phone_number_id"}
                    }
                )
            inherited = resolve_configuration(parent, 0, **bound).document.model_dump(mode="json")
            effective = (
                resolve_configuration(published[1], published[0], **bound) if published else None
            )

            def values(source: dict[str, JsonValue]) -> dict[str, JsonValue]:
                result: dict[str, JsonValue] = {}
                for field in SCOPE_FIELDS[kind]:
                    value: JsonValue = source
                    for key in field.split("."):
                        if isinstance(value, dict):
                            value = value.get(key)
                    result[field] = value
                return result

            return ConfigurationScopeView.model_validate(
                {
                    **view.model_dump(),
                    "value": own,
                    "published_value": published[1]
                    .scopes.model_dump(mode="json")[kind + "s"]
                    .get(str(identity))
                    if published
                    else None,
                    "kind": kind,
                    "id": identity,
                    "name": resource.name,
                    "is_active": resource.is_active
                    if isinstance(resource, ConfigurationResource)
                    else True,
                    "inherited_values": values(cast(dict[str, JsonValue], inherited)),
                    "effective_values": values(
                        cast(dict[str, JsonValue], effective.document.model_dump(mode="json"))
                    )
                    if effective
                    else {},
                    "field_origins": {
                        field: origin
                        for field, origin in effective.field_origins.items()
                        if any(
                            field == prefix or field.startswith(prefix + ".")
                            for prefix in SCOPE_FIELDS[kind]
                        )
                    }
                    if effective
                    else {},
                }
            )

    async def _validate(
        self, session: AsyncSession, row: ConfigurationDraft
    ) -> ConfigurationValidation:
        document = OperationalConfiguration.model_validate(row.document)
        issues: list[ValidationIssue] = []

        def issue(field: str, code: str, message: str) -> None:
            issues.append(ValidationIssue(field=field, code=code, message=message))

        if not document.business.name:
            issue("business.name", "required", "Set the business name.")
        if document.voice.enabled and not document.hours.windows:
            issue("hours.windows", "required", "Set business hours before enabling voice.")
        if document.hours.after_hours == "transfer" and not document.voice.transfer_number:
            issue("voice.transfer_number", "required", "Set an after-hours transfer destination.")
        if document.chat.enabled and not document.chat.allowed_origins:
            issue("chat.allowed_origins", "required", "Set the permitted widget domains.")
        ids = {
            identity
            for identity in (
                document.voice.primary_connection_id,
                document.voice.fallback_connection_id,
                *document.scheduling.calendar_connection_ids,
                *document.messaging.connection_ids,
            )
            if identity is not None
        }
        for identity in ids:
            connection = await session.scalar(
                select(TenantIntegration).where(
                    TenantIntegration.tenant_id == row.tenant_id, TenantIntegration.id == identity
                )
            )
            if connection is None or connection.status != "verified":
                issue(
                    "integrations",
                    "connection_unavailable",
                    "Every referenced connection must belong to this workspace and be verified.",
                )
        if document.voice.enabled:
            if document.voice.primary_connection_id is None:
                issue(
                    "voice.primary_connection_id", "required", "Select a verified phone connection."
                )
            if document.budget.monthly_budget_paisa <= 0:
                issue(
                    "budget.monthly_budget_paisa", "required", "Set an explicit paid-usage budget."
                )
        # A draft may describe pending capabilities; it cannot activate an absent adapter.
        values = document.model_dump()
        for field, reason in UNAVAILABLE_FEATURES.items():
            section, key = field.split(".")
            if values[section][key]:
                issue(field, "capability_unavailable", reason)
        for key, slot in (
            ("primary_connection_id", "primary"),
            ("fallback_connection_id", "fallback"),
        ):
            identity = getattr(document.voice, key)
            if identity is not None:
                connection = await session.scalar(
                    select(TenantIntegration).where(
                        TenantIntegration.tenant_id == row.tenant_id,
                        TenantIntegration.id == identity,
                    )
                )
                if connection is not None and (
                    connection.purpose != "telephony" or connection.slot != slot
                ):
                    issue(
                        f"voice.{key}",
                        "connection_incompatible",
                        "Select the matching verified phone connection.",
                    )
        issues.extend(await self._validate_scopes(session, row, document))
        from app.business.service import validate_directory_references

        try:
            await validate_directory_references(session, row.tenant_id, document.directory)
        except ConflictError:
            issue(
                "directory",
                "reference_unavailable",
                "Review business assignments and active workspace references.",
            )
        return ConfigurationValidation(revision=row.revision, valid=not issues, issues=issues)

    async def _validate_scopes(
        self, session: AsyncSession, row: ConfigurationDraft, document: OperationalConfiguration
    ) -> list[ValidationIssue]:
        from app.calls.models import PhoneNumber
        from app.locations.models import Location

        issues: list[ValidationIssue] = []

        def issue(field: str, message: str) -> None:
            issues.append(ValidationIssue(field=field, code="scope_unavailable", message=message))

        locations = set(document.scopes.locations)
        agents = set(document.scopes.agents)
        for scope in (*document.scopes.agents.values(), *document.scopes.channels.values()):
            if scope.location_id is not None:
                locations.add(scope.location_id)
        for scope in document.scopes.channels.values():
            if scope.agent_id is not None:
                agents.add(scope.agent_id)
        for identity in locations:
            if await session.get(Location, identity) is None:
                issue(
                    f"scopes.locations.{identity}", "Select a location belonging to this workspace."
                )
        for kind, identities in (("agent", agents), ("channel", set(document.scopes.channels))):
            for identity in identities:
                resource = await session.get(ConfigurationResource, identity)
                if resource is None or resource.kind != kind or not resource.is_active:
                    issue(
                        f"scopes.{kind}s.{identity}",
                        "Select an active resource belonging to this workspace.",
                    )
                elif (
                    kind == "channel"
                    and resource.channel_kind != document.scopes.channels[identity].kind
                ):
                    issue(
                        f"scopes.channels.{identity}.kind",
                        "Channel settings must match the resource kind.",
                    )
        for identity, scope in document.scopes.channels.items():
            if document.voice.enabled and scope.kind == "voice" and scope.phone_number_id is None:
                issue(
                    f"scopes.channels.{identity}.phone_number_id",
                    "Voice routing requires a workspace phone number.",
                )
            if scope.phone_number_id is not None:
                phone = await session.get(PhoneNumber, scope.phone_number_id)
                if phone is None or not phone.is_active:
                    issue(
                        f"scopes.channels.{identity}.phone_number_id",
                        "Select an active workspace phone number.",
                    )
        selections = [("location", identity) for identity in document.scopes.locations]
        selections.extend(("agent", identity) for identity in document.scopes.agents)
        selections.extend(("channel", identity) for identity in document.scopes.channels)
        for kind, identity in selections:
            try:
                effective = resolve_configuration(document, 0, **{f"{kind}_id": identity}).document
            except ValueError:
                issue(
                    f"scopes.{kind}s.{identity}",
                    "Override values are incompatible with inherited settings.",
                )
                continue
            if effective.hours.after_hours == "transfer" and not effective.voice.transfer_number:
                issue(
                    f"scopes.{kind}s.{identity}",
                    "After-hours transfer requires an effective destination.",
                )
            for field, reason in UNAVAILABLE_FEATURES.items():
                section, key = field.split(".")
                if getattr(getattr(effective, section), key) and not getattr(
                    getattr(document, section), key
                ):
                    issue(f"scopes.{kind}s.{identity}.{field}", reason)
        return issues

    async def validate(self, context: RequestContext, revision: int) -> ConfigurationValidation:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.CONFIGURATION_READ, lock=True)
            row = await self._draft(session, tenant)
            if row.revision != revision:
                raise RevisionConflictError()
            return await self._validate(session, row)

    async def diff(self, context: RequestContext, revision: int) -> ConfigurationDiff:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.CONFIGURATION_READ, lock=True)
            row = await self._draft(session, tenant)
            if row.revision != revision:
                raise RevisionConflictError()
            published = await published_for_tenant(session, tenant.id)
            changes = configuration_changes(
                published[1] if published is not None else None,
                OperationalConfiguration.model_validate(row.document),
            )
            from app.locations.models import Location

            for change in changes:
                parts = change.field.split(".")
                if len(parts) > 3 and parts[0] == "scopes":
                    identity = UUID(parts[2])
                    resource = await session.get(
                        Location if parts[1] == "locations" else ConfigurationResource, identity
                    )
                    name = resource.name if resource is not None else "Unavailable resource"
                    change.label = (
                        f"{parts[1][:-1].title()}: {name} · {' '.join(parts[3:]).replace('_', ' ')}"
                    )
            return ConfigurationDiff(
                revision=row.revision,
                published_version=published[0] if published is not None else None,
                changes=changes,
                validation=await self._validate(session, row),
                impact=[
                    "New conversations use published behavioral settings; existing conversations "
                    "retain their pinned behavioral revision.",
                    "Unavailable channels and workflows cannot be activated by publication.",
                    "This review compares the saved draft; unsaved edits are excluded.",
                ],
            )

    async def publish(
        self, context: RequestContext, revision: int, *, section: ConfigurationSection | None = None
    ) -> ConfigurationVersionView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(
                session,
                context,
                authority(section, "publish", context)
                if section
                else Permission.CONFIGURATION_PUBLISH,
                lock=True,
            )
            row = await self._draft(session, tenant)
            if row.revision != revision:
                raise RevisionConflictError()
            if section is not None:
                published = await published_for_tenant(session, tenant.id)
                if published is None:
                    raise InitialPublicationRequiredError()
                changes = configuration_changes(
                    published[1], OperationalConfiguration.model_validate(row.document)
                )
                if any(not in_publication_area(section, change.field) for change in changes):
                    raise SectionPublicationConflictError()
            if row.published_version is not None:
                previous = await session.scalar(
                    select(PublishedConfiguration).where(
                        PublishedConfiguration.tenant_id == tenant.id,
                        PublishedConfiguration.version == row.published_version,
                    )
                )
                if previous is not None and previous.source_revision == row.revision:
                    return ConfigurationVersionView(
                        version=previous.version,
                        source_revision=previous.source_revision,
                        content_hash=previous.content_hash,
                        document=OperationalConfiguration.model_validate(previous.document),
                    )
            validation = await self._validate(session, row)
            if not validation.valid:
                raise ConflictError()
            document = OperationalConfiguration.model_validate(row.document)
            version = (row.published_version or 0) + 1
            canonical = json.dumps(row.document, sort_keys=True, separators=(",", ":"))
            snapshot = PublishedConfiguration(
                tenant_id=tenant.id,
                version=version,
                source_revision=row.revision,
                document=row.document,
                content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                published_by=context.actor_id,
                platform_user_id=context.platform_user_id,
            )
            session.add(snapshot)
            await session.flush()
            session.add(
                ConfigurationEvent(
                    tenant_id=tenant.id, configuration_id=snapshot.id, version=version
                )
            )
            row.published_version = version
            tenant.name = document.business.name
            tenant.language_policy = document.languages.model_dump(mode="json")
            tenant.human_transfer_number = document.voice.transfer_number
            audit(session, context, "configuration_published", "configuration", snapshot.id)
            return ConfigurationVersionView(
                version=version,
                source_revision=row.revision,
                content_hash=snapshot.content_hash,
                document=document,
            )

    async def test(
        self, context: RequestContext, command: ConfigurationTest
    ) -> ConfigurationTestView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.CONFIGURATION_TEST, lock=True)
            row = await self._draft(session, tenant)
            if row.revision != command.revision:
                raise RevisionConflictError()
            document = OperationalConfiguration.model_validate(row.document)
            audit(session, context, "configuration_tested", "configuration", row.id)
            return ConfigurationTestView(
                revision=row.revision,
                validation=await self._validate(session, row),
                instant=command.instant,
                business_open=is_business_open(document, command.instant),
                timezone=document.business.timezone,
                greeting=document.agent.greeting,
                language=document.languages.default_language,
                tools=document.agent.allowed_tools,
                duration_minutes=document.voice.max_duration_minutes,
            )

    async def effective(
        self,
        context: RequestContext,
        *,
        location_id: UUID | None = None,
        agent_id: UUID | None = None,
        channel_id: UUID | None = None,
    ) -> EffectiveConfigurationView:
        async with self.sessions.begin() as session:
            await self._enter(session, context, Permission.CONFIGURATION_READ)
            from app.locations.models import Location

            if location_id and await session.get(Location, location_id) is None:
                raise NotFoundError()
            for kind, identity in (("agent", agent_id), ("channel", channel_id)):
                if identity:
                    resource = await session.get(ConfigurationResource, identity)
                    if resource is None or resource.kind != kind:
                        raise NotFoundError()
            published = await published_for_tenant(session, context.tenant_id)
            if published is None:
                raise NotFoundError()
            return resolve_configuration(
                published[1],
                published[0],
                location_id=location_id,
                agent_id=agent_id,
                channel_id=channel_id,
            )

    async def history(self, context: RequestContext) -> list[ConfigurationVersionView]:
        async with self.sessions.begin() as session:
            await self._enter(session, context, Permission.CONFIGURATION_READ)
            rows = (
                await session.scalars(
                    select(PublishedConfiguration)
                    .where(PublishedConfiguration.tenant_id == context.tenant_id)
                    .order_by(PublishedConfiguration.version.desc())
                    .limit(100)
                )
            ).all()
            return [
                ConfigurationVersionView(
                    version=row.version,
                    source_revision=row.source_revision,
                    content_hash=row.content_hash,
                    document=OperationalConfiguration.model_validate(row.document),
                )
                for row in rows
            ]

    async def rollback(
        self, context: RequestContext, version: int, revision: int
    ) -> ConfigurationView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(
                session, context, Permission.CONFIGURATION_ROLLBACK, lock=True
            )
            row = await self._draft(session, tenant)
            if row.revision != revision:
                raise RevisionConflictError()
            old = await session.scalar(
                select(PublishedConfiguration).where(
                    PublishedConfiguration.tenant_id == tenant.id,
                    PublishedConfiguration.version == version,
                )
            )
            if old is None:
                raise NotFoundError()
            row.document = OperationalConfiguration.model_validate(old.document).model_dump(
                mode="json"
            )
            from app.configuration.hours import sync_hours

            await sync_hours(
                session, tenant.id, OperationalConfiguration.model_validate(row.document)
            )
            from app.business.persistence import sync_directory

            await sync_directory(
                session, tenant.id, OperationalConfiguration.model_validate(row.document).directory
            )
            row.revision += 1
            audit(session, context, "configuration_rollback_staged", "configuration", old.id)
            return self._view(row)


async def published_for_tenant(
    session: AsyncSession, tenant_id: UUID
) -> tuple[int, OperationalConfiguration] | None:
    """Read authoritative storage at admission; no stale process-level configuration cache."""
    row = await session.scalar(
        select(PublishedConfiguration)
        .where(PublishedConfiguration.tenant_id == tenant_id)
        .order_by(PublishedConfiguration.version.desc())
        .limit(1)
    )
    if row is None:
        return None
    return row.version, OperationalConfiguration.model_validate(row.document)


def configuration_catalog() -> dict[str, object]:
    from app.conversation.locales import LOCALES
    from app.rbac.catalog import permission_catalog

    return {
        "version": 1,
        "defaults": {
            "version": DEFAULTS_VERSION,
            "document": tenant_defaults().model_dump(mode="json"),
        },
        "schema": OperationalConfiguration.model_json_schema(),
        "permissions": permission_catalog(),
        "sections": {
            name: {
                "read_permission": owner.read.value,
                "update_permission": owner.update.value,
                "publish_permission": owner.publish.value,
            }
            for name, owner in SECTION_AUTHORITY.items()
        },
        "languages": [{"code": key, "name": locale.name} for key, locale in LOCALES.items()],
        "tools": sorted(ALL_TOOLS),
        "workflow_types": [
            "appointment_reminders",
            "missed_call_follow_up",
            "consented_callbacks",
            "lead_assignment",
            "ticket_escalation",
            "post_call_sync",
        ],
        "template_variables": [
            "starts_at",
            "ends_at",
            "business_name",
            "service_name",
            "location_name",
        ],
        "unavailable_fields": UNAVAILABLE_FEATURES,
        "scope_resources": [],
        "locations": [],
        "scope_fields": SCOPE_FIELDS,
        "scope_sections": SCOPE_SECTION,
        "scope_schemas": {
            "location": "LocationOverride",
            "agent": "AgentOverride",
            "channel": "ChannelOverride",
        },
        "scope_bindings": {
            "location": {
                "timezone": "business.timezone",
                "address": "business.address",
                "hours": "hours",
            },
            "agent": {field.split(".")[-1]: field for field in SCOPE_FIELDS["agent"]},
            "channel": {
                "greeting": "agent.greeting",
                "max_duration_minutes": "voice.max_duration_minutes",
                "transfer_number": "voice.transfer_number",
                "chat": "chat",
            },
        },
        "channel_kinds": ["voice", "chat"],
        "phone_numbers": [],
        "resource_limits": {
            "agent": 200,
            "channel": 200,
            "scope_write_batch": MAX_SCOPE_WRITE_BATCH,
        },
        "observed_at": datetime.now(UTC).isoformat(),
    }
