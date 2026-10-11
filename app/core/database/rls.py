from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TENANT_TABLES = (
    "tenants",
    "users",
    "audit_events",
    "locations",
    "providers",
    "patients",
    "appointments",
    "idempotency_records",
    "outbox_events",
    "phone_numbers",
    "calls",
    "documents",
    "document_versions",
    "document_chunks",
    "knowledge_jobs",
    "workflow_intents",
    "call_transcripts",
    "post_call_reports",
    "conversations",
    "conversation_messages",
    "tickets",
    "ticket_events",
    "ai_turns",
    "tenant_onboardings",
    "tenant_integrations",
    "integration_credential_versions",
    "tenant_roles",
    "role_permissions",
    "tenant_settings",
    "settings_changes",
    "services",
    "provider_schedules",
    "provider_time_off",
    "outbound_messages",
    "calendar_connections",
    "calendar_oauth_states",
    "calendar_busy_blocks",
    "appointment_calendar_links",
)


class UnsafeDatabaseRoleError(RuntimeError):
    """The application must never silently run tenant queries with bypass privileges."""


async def verify_runtime_role(session: AsyncSession) -> None:
    unsafe = await session.scalar(
        text(
            "SELECT rolsuper OR rolbypassrls OR rolcreaterole "
            "FROM pg_roles WHERE rolname = current_user"
        )
    )
    if unsafe is not False:
        raise UnsafeDatabaseRoleError("Use a restricted application database role")
    safe_tables = await session.scalar(
        text("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = ANY(CAST(:tables AS text[]))
          AND c.relrowsecurity AND c.relforcerowsecurity
          AND NOT pg_has_role(current_user, c.relowner, 'MEMBER')
        """),
        {"tables": list(TENANT_TABLES)},
    )
    if safe_tables != len(TENANT_TABLES):
        raise UnsafeDatabaseRoleError("Migrate the schema and use a non-owner application role")
    can_read_domain = await session.scalar(
        text("SELECT has_table_privilege(current_user, 'public.patients', 'SELECT')")
    )
    if can_read_domain is not True:
        raise UnsafeDatabaseRoleError("Use the API login, not the outbox worker login")


async def set_tenant_scope(session: AsyncSession, tenant_id: UUID) -> None:
    """SET LOCAL equivalent with a bound parameter; cleared on commit AND rollback."""
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
