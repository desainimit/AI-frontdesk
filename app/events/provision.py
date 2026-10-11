import re

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database.session import Database
from app.core.settings import Settings
from app.events.store import OutboxStore


async def provision_worker(settings: Settings) -> None:
    if settings.worker_database_url is None:
        return
    worker = make_url(settings.worker_database_url.get_secret_value())
    runtime = make_url(settings.database_url.get_secret_value())
    owner = make_url(settings.migration_url)
    role = worker.username or ""
    if (
        role in {runtime.username, owner.username}
        or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role)
        or not worker.password
        or worker.database != owner.database
    ):
        raise ValueError("Worker must use a separate restricted login in the same database")
    engine = create_async_engine(settings.migration_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            existing = (
                await connection.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb "
                        "OR rolreplication FROM pg_roles WHERE rolname = :role"
                    ),
                    {"role": role},
                )
            ).first()
            if existing is None:
                sql = await connection.scalar(
                    text(
                        "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEROLE NOCREATEDB NOREPLICATION PASSWORD %L', "
                        "CAST(:role AS text), CAST(:password AS text))"
                    ),
                    {"role": role, "password": worker.password},
                )
                if not isinstance(sql, str):
                    raise RuntimeError("Could not prepare worker role creation")
                await connection.exec_driver_sql(sql)
            elif existing[0]:
                raise ValueError("Existing worker role has elevated privileges")
            quoted = engine.dialect.identifier_preparer.quote(role)
            await connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted}")
            await connection.exec_driver_sql(f"REVOKE ALL ON auth_mail FROM {quoted}")
            await connection.exec_driver_sql(f"GRANT SELECT ON auth_mail TO {quoted}")
            await connection.exec_driver_sql(
                f"GRANT UPDATE (status, ciphertext, attempts, available_at, lease_id, lease_until) "
                f"ON auth_mail TO {quoted}"
            )
            # Background jobs read the job tenant's own connection under ordinary tenant
            # RLS (app.tenant_id is set per claim); there is no cross-tenant worker policy.
            integration_tables = "tenant_integrations, integration_credential_versions"
            await connection.exec_driver_sql(f"REVOKE ALL ON {integration_tables} FROM {quoted}")
            await connection.exec_driver_sql(f"GRANT SELECT ON {integration_tables} TO {quoted}")
            await connection.exec_driver_sql(f"REVOKE ALL ON outbox_events FROM {quoted}")
            await connection.exec_driver_sql(
                f"GRANT SELECT ON platform_connections, platform_settings TO {quoted}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT, UPDATE ON platform_status TO {quoted}"
            )
            await connection.exec_driver_sql(f"GRANT SELECT ON outbox_events TO {quoted}")
            await connection.exec_driver_sql(
                "GRANT UPDATE (status, attempts, available_at, lease_token, lease_until, "
                f"delivered_at, last_error) ON outbox_events TO {quoted}"
            )
            # The worker sees only scheduling IDs globally. Document content tables
            # retain ordinary tenant RLS; processing sets app.tenant_id per claim.
            await connection.exec_driver_sql(f"GRANT SELECT, UPDATE ON knowledge_jobs TO {quoted}")
            await connection.exec_driver_sql(
                f"GRANT SELECT ON documents, document_versions, document_chunks TO {quoted}"
            )
            await connection.exec_driver_sql(f"GRANT INSERT, DELETE ON document_chunks TO {quoted}")
            await connection.exec_driver_sql(
                f"GRANT UPDATE (status, error_code, updated_at) ON document_versions TO {quoted}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, UPDATE ON workflow_intents TO {quoted}"
            )
            await connection.exec_driver_sql(f"GRANT SELECT ON call_transcripts TO {quoted}")
            await connection.exec_driver_sql(f"GRANT DELETE ON call_transcripts TO {quoted}")
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON post_call_reports TO {quoted}"
            )
            # The worker opens tickets for what it finds: a call that needs a person,
            # an appointment changed in a provider's own calendar.
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON tickets, ticket_events TO {quoted}"
            )
            for table in ("tickets", "ticket_events"):
                await connection.exec_driver_sql(f"DROP POLICY IF EXISTS ticket_opening ON {table}")
                await connection.exec_driver_sql(
                    f"CREATE POLICY ticket_opening ON {table} TO {quoted} "
                    "USING (true) WITH CHECK (true)"
                )
            await connection.exec_driver_sql(f"GRANT DELETE ON post_call_reports TO {quoted}")
            await connection.exec_driver_sql(
                f"REVOKE ALL ON tenants, calls, appointments FROM {quoted}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT (id, is_active, service_state) ON tenants TO {quoted}"
            )
            await connection.exec_driver_sql(
                "GRANT SELECT (id, tenant_id, status, ended_at, started_at, created_at, "
                f"received_frames, sent_frames) ON calls TO {quoted}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT (id, tenant_id, version, status, starts_at) "
                f"ON appointments TO {quoted}"
            )
            # The worker reads each clinic's settings to apply them (how long transcripts
            # are kept, and later the wording of messages). It cannot change them.
            await connection.exec_driver_sql(f"REVOKE ALL ON tenant_settings FROM {quoted}")
            await connection.exec_driver_sql(f"GRANT SELECT ON tenant_settings TO {quoted}")
            await connection.exec_driver_sql(
                "DROP POLICY IF EXISTS worker_settings_read ON tenant_settings"
            )
            await connection.exec_driver_sql(
                f"CREATE POLICY worker_settings_read ON tenant_settings FOR SELECT TO {quoted} "
                "USING (true)"
            )
            # Calendar sync runs here: read the connection, write busy time and event links.
            synced = "calendar_connections, calendar_busy_blocks, appointment_calendar_links"
            await connection.exec_driver_sql(f"REVOKE ALL ON {synced} FROM {quoted}")
            await connection.exec_driver_sql(f"GRANT SELECT ON calendar_connections TO {quoted}")
            await connection.exec_driver_sql(
                "GRANT UPDATE (status, last_synced_at, last_error, updated_at) "
                f"ON calendar_connections TO {quoted}"
            )
            await connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_busy_blocks, "
                f"appointment_calendar_links TO {quoted}"
            )
            for table in (
                "calendar_connections",
                "calendar_busy_blocks",
                "appointment_calendar_links",
            ):
                await connection.exec_driver_sql(f"DROP POLICY IF EXISTS calendar_sync ON {table}")
                await connection.exec_driver_sql(
                    f"CREATE POLICY calendar_sync ON {table} TO {quoted} "
                    "USING (true) WITH CHECK (true)"
                )
            await connection.exec_driver_sql(f"REVOKE ALL ON outbound_messages FROM {quoted}")
            await connection.exec_driver_sql(f"GRANT SELECT ON outbound_messages TO {quoted}")
            await connection.exec_driver_sql(
                "GRANT UPDATE (status, attempts, available_at, lease_token, lease_until, "
                f"sent_at, last_error, updated_at) ON outbound_messages TO {quoted}"
            )
            await connection.exec_driver_sql(
                "DROP POLICY IF EXISTS message_delivery ON outbound_messages"
            )
            await connection.exec_driver_sql(
                f"CREATE POLICY message_delivery ON outbound_messages TO {quoted} "
                "USING (true) WITH CHECK (true)"
            )
            await connection.exec_driver_sql(
                "DROP POLICY IF EXISTS knowledge_job_claim ON knowledge_jobs"
            )
            await connection.exec_driver_sql(
                f"CREATE POLICY knowledge_job_claim ON knowledge_jobs TO {quoted} "
                "USING (true) WITH CHECK (true)"
            )
            await connection.exec_driver_sql(
                "DROP POLICY IF EXISTS outbox_worker_delivery ON outbox_events"
            )
            await connection.exec_driver_sql(
                f"CREATE POLICY outbox_worker_delivery ON outbox_events TO {quoted} "
                "USING (true) WITH CHECK (true)"
            )
            await connection.exec_driver_sql(
                "DROP POLICY IF EXISTS workflow_intent_claim ON workflow_intents"
            )
            await connection.exec_driver_sql(
                f"CREATE POLICY workflow_intent_claim ON workflow_intents TO {quoted} "
                "USING (true) WITH CHECK (true)"
            )
    finally:
        await engine.dispose()
    database = Database(settings.model_copy(update={"database_url": settings.worker_database_url}))
    try:
        await OutboxStore(database.session_factory).check()
    finally:
        await database.close()
