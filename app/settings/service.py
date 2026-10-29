"""Clinic settings: read everything, save one section, see what changed.

A save takes effect at once. The receptionist reads the settings when a call starts,
so a call already in progress finishes with the wording it started with.
"""

from typing import cast

from pydantic import JsonValue, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.models import Call
from app.core.database.pages import decode_cursor, encode_cursor
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.settings import Settings
from app.integrations.resolver import TenantIntegrationResolver
from app.settings.models import SettingsChange, TenantSettings
from app.settings.schemas import (
    SECTION_MODELS,
    ClinicConfiguration,
    FieldChange,
    SettingsChangePage,
    SettingsChangeView,
    SettingsSection,
    SettingsView,
)
from app.tenants.models import Tenant
from app.users.models import User
from app.voice.factory import VoiceLanguageError, validate_voices


def field_changes(before: JsonValue, after: JsonValue, field: str = "") -> list[FieldChange]:
    """Leaf fields that differ. Lists are compared as a whole: hours change together."""
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[FieldChange] = []
        for key in sorted(set(before) | set(after)):
            path = f"{field}.{key}" if field else key
            changes.extend(field_changes(before.get(key), after.get(key), path))
        return changes
    if before == after:
        return []
    return [FieldChange(field=field, before=before, after=after)]


class SettingsService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], settings: Settings | None = None
    ) -> None:
        self._sessions = sessions
        self._settings = settings

    @staticmethod
    def _view(row: TenantSettings) -> SettingsView:
        return SettingsView(
            version=row.version,
            updated_at=row.updated_at,
            settings=ClinicConfiguration.model_validate(row.document),
        )

    async def get(self, context: RequestContext) -> SettingsView:
        require_permission(context, Permission.SETTINGS_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            row = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == context.tenant_id)
            )
            if row is None:
                raise NotFoundError()
            return self._view(row)

    async def save(
        self, context: RequestContext, section: SettingsSection, value: JsonValue
    ) -> SettingsView:
        require_permission(context, Permission.SETTINGS_UPDATE)
        stt = tts = None
        if self._settings is not None and section in {
            SettingsSection.RECEPTIONIST,
            SettingsSection.CALLS,
        }:
            # Resolve before taking the settings lock: test and small deployments
            # may have a one-connection pool. Runtime validates the pair again.
            resolver = TenantIntegrationResolver(self._sessions, self._settings)
            stt = await resolver.active(context.tenant_id, "stt", fresh=True)
            tts = await resolver.active(context.tenant_id, "tts", fresh=True)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            row = await session.scalar(
                select(TenantSettings)
                .where(TenantSettings.tenant_id == context.tenant_id)
                .with_for_update()
            )
            if row is None:
                raise NotFoundError()
            try:
                saved = SECTION_MODELS[section].model_validate(value).model_dump(mode="json")
            except ValidationError as failure:
                raise ValidationFailedError.from_pydantic(failure) from None
            if self._settings is not None and section in {
                SettingsSection.RECEPTIONIST,
                SettingsSection.CALLS,
            }:
                configuration = ClinicConfiguration.model_validate(
                    {**row.document, section.value: saved}
                )
                policy = configuration.receptionist.languages
                # Every offered language must be understood and spoken on calls.
                if configuration.calls.enabled:
                    try:
                        validate_voices(self._settings, policy, stt, tts)
                    except VoiceLanguageError as failure:
                        raise ValidationFailedError(failure.issues) from None
            before = cast(JsonValue, row.document[section.value])
            changes = field_changes(before, cast(JsonValue, saved))
            if not changes:
                return self._view(row)
            row.document = {**row.document, section.value: saved}
            row.version += 1
            if section is SettingsSection.CLINIC:
                tenant = await session.get(Tenant, context.tenant_id)
                if tenant is not None:
                    tenant.name = saved["name"]
            if any(change.field.startswith("caller_verification") for change in changes):
                # A stricter rule must not leave callers verified under the old one.
                await session.execute(
                    update(Call)
                    .where(Call.tenant_id == context.tenant_id, Call.status == "active")
                    .values(
                        access_patient_id=None,
                        verified_patient_id=None,
                        access_digest=None,
                        access_salt=None,
                        access_expires_at=None,
                        verification_expires_at=None,
                        access_attempts=0,
                    )
                )
            session.add(
                SettingsChange(
                    tenant_id=context.tenant_id,
                    section=section.value,
                    version=row.version,
                    changed_by=await self._actor_name(session, context),
                    actor_id=context.actor_id,
                    changes=[change.model_dump(mode="json") for change in changes],
                )
            )
            session.add(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    actor_type=context.actor_type,
                    platform_user_id=context.platform_user_id,
                    support_session_id=context.support_session_id,
                    action=f"settings_{section.value}_updated",
                    resource_type="settings",
                    resource_id=row.id,
                    request_id=context.request_id,
                )
            )
            await session.flush()
            await session.refresh(row)
            return self._view(row)

    async def history(
        self, context: RequestContext, limit: int, cursor: str | None
    ) -> SettingsChangePage:
        require_permission(context, Permission.SETTINGS_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            offset = decode_cursor(cursor)
            rows = (
                await session.scalars(
                    select(SettingsChange)
                    .where(SettingsChange.tenant_id == context.tenant_id)
                    .order_by(SettingsChange.created_at.desc(), SettingsChange.id)
                    .limit(limit + 1)
                    .offset(offset)
                )
            ).all()
            return SettingsChangePage(
                items=[
                    SettingsChangeView.model_validate(
                        {
                            "id": row.id,
                            "section": row.section,
                            "version": row.version,
                            "changed_by": row.changed_by,
                            "created_at": row.created_at,
                            "changes": row.changes,
                        }
                    )
                    for row in rows[:limit]
                ],
                next_cursor=encode_cursor(offset + limit) if len(rows) > limit else None,
            )

    @staticmethod
    async def _actor_name(session: AsyncSession, context: RequestContext) -> str:
        if context.actor_type == ActorType.USER:
            name = await session.scalar(
                select(User.display_name).where(User.id == context.actor_id)
            )
            return name or "A team member"
        return "AI Frontdesk support"
