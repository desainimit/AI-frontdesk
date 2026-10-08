"""Operator provisioning for the isolated financial database login."""

import asyncio
import re

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.billing.database import FinancialDatabase, verify_financial_role
from app.config.settings import Settings
from app.database.provision import ProvisioningConfigurationError
from app.database.rls import UnsafeDatabaseRoleError


async def provision_financial_role(settings: Settings) -> None:
    if not settings.financial_database_url or not settings.migration_database_url:
        raise ProvisioningConfigurationError(
            "Set FINANCIAL_DATABASE_URL and MIGRATION_DATABASE_URL."
        )
    financial = make_url(settings.financial_database_url.get_secret_value())
    owner = make_url(settings.migration_url)
    api = make_url(settings.database_url.get_secret_value())
    worker = (
        make_url(settings.worker_database_url.get_secret_value()).username
        if settings.worker_database_url
        else None
    )
    role = financial.username or ""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role) or not financial.password:
        raise ProvisioningConfigurationError("Use a dedicated financial login with a password.")
    if role in {owner.username, api.username, worker} or (
        financial.host,
        financial.port,
        financial.database,
    ) != (owner.host, owner.port, owner.database):
        raise ProvisioningConfigurationError(
            "Use a separate financial login for the same migrated database."
        )
    engine = create_async_engine(settings.migration_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            existing = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
                        "rolreplication FROM pg_roles WHERE rolname = :role"
                    ),
                    {"role": role},
                )
            ).first()
            if existing is None:
                statement = await connection.scalar(
                    text(
                        "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEROLE NOCREATEDB NOREPLICATION PASSWORD %L', "
                        "CAST(:role AS text), CAST(:password AS text))"
                    ),
                    {"role": role, "password": financial.password},
                )
                if not isinstance(statement, str):
                    raise RuntimeError("Could not prepare financial login")
                await connection.exec_driver_sql(statement)
            elif any(existing):
                raise ProvisioningConfigurationError(
                    "The financial login must have no elevated privileges."
                )
            quoted = engine.dialect.identifier_preparer.quote(role)
            # Refuse an existing clinical/owner login before granting any financial writes.
            await connection.exec_driver_sql(f"SET LOCAL ROLE {quoted}")
            await verify_financial_role(connection)
            await connection.exec_driver_sql("RESET ROLE")
            await connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted}")
            await connection.exec_driver_sql(
                "GRANT SELECT, INSERT ON billing_accounts, billing_ledger, billing_mutations, "
                f"billing_plan_versions, usage_reservations, usage_commands TO {quoted}"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (revision, balance_minor, reserved_minor, plan_version_id, "
                f"period_start, period_end, updated_at) ON billing_accounts TO {quoted}"
            )
            # Only the columns needed by pricing/adjustment authority RLS.
            await connection.exec_driver_sql(
                "GRANT UPDATE (revision, state, actual_units, lease_expires_at, fence, "
                "settled_at, outcome_reference, updated_at) "
                f"ON usage_reservations TO {quoted}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT (id, is_active, role) ON platform_users TO {quoted}"
            )
            await connection.exec_driver_sql(
                "GRANT SELECT (id, user_id, expires_at, revoked_at, mfa_verified_at) "
                f"ON platform_sessions TO {quoted}"
            )
    finally:
        await engine.dispose()
    database = FinancialDatabase(settings)
    try:
        await database.ping()
    finally:
        await database.close()


def main() -> None:
    try:
        asyncio.run(provision_financial_role(Settings.from_environment()))
    except (ValidationError, ValueError, SQLAlchemyError, UnsafeDatabaseRoleError):
        # Role creation statements contain an escaped password. Never echo SQL or URLs.
        raise SystemExit(
            "Financial login provisioning failed. Verify migrations, owner access and a "
            "dedicated FINANCIAL_DATABASE_URL for the same database. "
            "The financial login must not have unrelated data or elevated privileges."
        ) from None


if __name__ == "__main__":
    main()
