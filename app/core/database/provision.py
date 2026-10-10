"""Operator command: provision a least-privilege login after running migrations."""

import asyncio
import re

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database.session import Database
from app.core.settings import Settings
from app.events.provision import provision_worker


class ProvisioningConfigurationError(ValueError):
    """Static operator-facing guidance; never include connection URLs or passwords."""


def validate_role_configuration(settings: Settings) -> str:
    runtime = make_url(settings.database_url.get_secret_value())
    migration = make_url(settings.migration_url)
    role = runtime.username or ""
    if not settings.migration_database_url:
        raise ProvisioningConfigurationError(
            "Set MIGRATION_DATABASE_URL to the schema owner connection."
        )
    if role == migration.username:
        raise ProvisioningConfigurationError(
            "DATABASE_URL and MIGRATION_DATABASE_URL must use different PostgreSQL users. "
            "Use a restricted application login for DATABASE_URL and the schema owner "
            "for MIGRATION_DATABASE_URL."
        )
    if runtime.database != migration.database:
        raise ProvisioningConfigurationError("Both connection URLs must name the same database.")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ProvisioningConfigurationError(
            "The runtime username must start with a lowercase letter or underscore and contain "
            "only lowercase letters, digits, or underscores (maximum 63 characters)."
        )
    if not runtime.password:
        raise ProvisioningConfigurationError("Set the runtime login password in DATABASE_URL.")
    return role


async def provision_runtime_role(settings: Settings) -> None:
    role = validate_role_configuration(settings)
    runtime = make_url(settings.database_url.get_secret_value())
    engine = create_async_engine(settings.migration_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            existing = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
                        "FROM pg_roles WHERE rolname = :name"
                    ),
                    {"name": role},
                )
            ).first()
            if existing is None:
                # PostgreSQL formats identifiers/literals safely; secrets never enter log messages.
                statement = await connection.scalar(
                    text(
                        "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEROLE NOCREATEDB NOREPLICATION PASSWORD %L', "
                        "CAST(:name AS text), CAST(:password AS text))"
                    ),
                    {"name": role, "password": runtime.password},
                )
                if not isinstance(statement, str):
                    raise RuntimeError("Could not prepare role creation")
                await connection.exec_driver_sql(statement)
            elif any(existing):
                raise ProvisioningConfigurationError(
                    "Existing runtime role has elevated privileges; choose a dedicated role"
                )
            quoted_role = engine.dialect.identifier_preparer.quote(role)
            await connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
            await connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_roles, role_permissions "
                f"TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT, UPDATE ON tenant_settings, services TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON settings_changes TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON provider_schedules, provider_time_off "
                f"TO {quoted_role}"
            )
            # Platform provider accounts hold ciphertext only; the keyring is not here.
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON platform_connections TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, UPDATE ON platform_settings TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"REVOKE ALL ON tenants, users, audit_events FROM {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT ON tenants, users, audit_events TO {quoted_role}"
            )
            await connection.exec_driver_sql(f"GRANT INSERT ON audit_events TO {quoted_role}")
            await connection.exec_driver_sql(f"GRANT INSERT ON tenants, users TO {quoted_role}")
            await connection.exec_driver_sql(
                f"GRANT UPDATE (name, is_active, updated_at, service_state) ON tenants "
                f"TO {quoted_role}"
            )
            integration_tables = "tenant_integrations, integration_credential_versions"
            await connection.exec_driver_sql(
                f"REVOKE ALL ON {integration_tables} FROM {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON {integration_tables} TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (provider, account_identifier, config, status, revision, "
                f"active_version_id, last_checked_at, updated_at) "
                f"ON tenant_integrations TO {quoted_role}"
            )
            # Version rows are immutable apart from lifecycle state and secret erasure.
            await connection.exec_driver_sql(
                "GRANT UPDATE (state, ciphertext, error_code, validated_at, lease_token, "
                f"lease_until) ON integration_credential_versions TO {quoted_role}"
            )
            knowledge_tables = "documents, document_versions, document_chunks, knowledge_jobs"
            await connection.exec_driver_sql(f"REVOKE ALL ON {knowledge_tables} FROM {quoted_role}")
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON {knowledge_tables} TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT UPDATE (name, source_type, status, active_version, updated_at) "
                f"ON documents TO {quoted_role}"
            )
            domain_tables = "locations, providers, patients, appointments"
            await connection.exec_driver_sql(
                f"REVOKE ALL ON phone_numbers, calls FROM {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT, UPDATE ON phone_numbers, calls TO {quoted_role}"
            )
            await connection.exec_driver_sql(f"REVOKE ALL ON {domain_tables} FROM {quoted_role}")
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON {domain_tables} TO {quoted_role}"
            )
            await connection.exec_driver_sql(f"GRANT UPDATE ON appointments TO {quoted_role}")
            await connection.exec_driver_sql(
                "GRANT UPDATE (name, timezone, address, phone, updated_at) ON locations "
                f"TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (location_id, display_name, specialty, is_active, updated_at) "
                f"ON providers TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (email, display_name, is_active, role_id, updated_at, "
                "account_id, accepted_at) "
                f"ON users TO {quoted_role}"
            )
            await connection.exec_driver_sql(f"REVOKE ALL ON tenant_onboardings FROM {quoted_role}")
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON tenant_onboardings TO {quoted_role}"
            )
            # Sign-in data. The API login can never make an account a platform
            # administrator: that column is outside both its INSERT and UPDATE rights.
            await connection.exec_driver_sql(
                f"REVOKE ALL ON accounts, account_mfa, sessions FROM {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT ON accounts, account_mfa, sessions TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                "GRANT INSERT (id, email, display_name, password_hash, credential_revision, "
                f"is_active) ON accounts TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (display_name, password_hash, credential_revision, updated_at) "
                f"ON accounts TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT INSERT, UPDATE, DELETE ON account_mfa TO {quoted_role}"
            )
            await connection.exec_driver_sql(f"GRANT INSERT ON sessions TO {quoted_role}")
            await connection.exec_driver_sql(
                "GRANT UPDATE (expires_at, revoked_at, credential_revision, mfa_pending, "
                "mfa_verified_at, tenant_id, support_id, support_reason, support_expires_at) "
                f"ON sessions TO {quoted_role}"
            )
            auth_tables = "auth_challenges, auth_mail"
            await connection.exec_driver_sql(f"REVOKE ALL ON {auth_tables} FROM {quoted_role}")
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON {auth_tables} TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT UPDATE (consumed_at) ON auth_challenges TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT UPDATE (status, ciphertext) ON auth_mail TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT UPDATE (date_of_birth, account_identifier, updated_at) "
                f"ON patients TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"REVOKE ALL ON idempotency_records, outbox_events FROM {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON idempotency_records, outbox_events TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON workflow_intents TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, INSERT ON call_transcripts TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT UPDATE (snapshot) ON call_transcripts TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT ON post_call_reports, follow_up_events TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT UPDATE (status, acknowledged_at) ON follow_up_events TO {quoted_role}"
            )
            # Activity log: the API login adds rows and can read nothing back but the
            # timestamp the database assigns (which an ORM insert returns).
            await connection.exec_driver_sql(
                f"REVOKE ALL ON platform_audit_events FROM {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT INSERT ON platform_audit_events TO {quoted_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT (created_at) ON platform_audit_events TO {quoted_role}"
            )
    finally:
        await engine.dispose()
    database = Database(settings)
    try:
        await database.ping()
    finally:
        await database.close()
    await provision_worker(settings)


def main() -> None:
    try:
        asyncio.run(provision_runtime_role(Settings.from_environment()))
    except ProvisioningConfigurationError as exc:
        raise SystemExit(f"Role provisioning failed: {exc}") from None
    except (SQLAlchemyError, ValueError, RuntimeError):
        raise SystemExit(
            "Role provisioning failed. Verify separate credentials, role privileges, "
            "database connectivity, and applied migrations. No existing password is rotated."
        ) from None
    print("Restricted runtime role provisioned and verified.")


if __name__ == "__main__":
    main()
