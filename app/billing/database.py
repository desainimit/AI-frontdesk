"""A separate, narrow financial login; never the ordinary application login."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.database.rls import UnsafeDatabaseRoleError
from app.database.session import Database
from app.errors import ApplicationError

FINANCIAL_TABLES = (
    "billing_accounts",
    "billing_ledger",
    "billing_mutations",
    "billing_plan_versions",
    "usage_reservations",
    "usage_commands",
)


class FinancialUnavailableError(ApplicationError):
    status_code = 503
    code = "financial_unavailable"
    public_message = (
        "Financial commands are temporarily unavailable. Your existing balance is preserved."
    )


async def verify_financial_role(session: AsyncSession | AsyncConnection) -> None:
    safe = await session.scalar(
        text(
            "SELECT NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb "
            "OR rolreplication) FROM pg_roles WHERE rolname = current_user"
        )
    )
    count = await session.scalar(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:tables AS text[])) "
            "AND c.relrowsecurity AND c.relforcerowsecurity "
            "AND NOT pg_has_role(current_user, c.relowner, 'MEMBER')"
        ),
        {"tables": list(FINANCIAL_TABLES)},
    )
    too_broad = await session.scalar(
        text(
            "SELECT coalesce(bool_or(has_table_privilege(current_user, c.oid, "
            "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') OR "
            "has_any_column_privilege(current_user, c.oid, 'SELECT,INSERT,UPDATE')), false) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm') "
            "AND NOT (c.relname = ANY(CAST(:allowed AS text[])))"
        ),
        {"allowed": [*FINANCIAL_TABLES, "platform_users", "platform_sessions"]},
    )
    immutable = await session.scalar(
        text(
            "SELECT bool_and(NOT (has_table_privilege(current_user, c.oid, "
            "'UPDATE,DELETE,TRUNCATE') OR "
            "has_any_column_privilege(current_user, c.oid, 'UPDATE'))) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname IN "
            "('billing_ledger', 'billing_mutations', 'billing_plan_versions', 'usage_commands')"
        )
    )
    reads_secrets = await session.scalar(
        text(
            "SELECT has_column_privilege(current_user, 'platform_users', "
            "'password_hash', 'SELECT') "
            "OR has_column_privilege(current_user, 'platform_sessions', 'token_digest', 'SELECT')"
        )
    )
    if (
        safe is not True
        or count != len(FINANCIAL_TABLES)
        or too_broad is not False
        or immutable is not True
        or reads_secrets is not False
    ):
        raise UnsafeDatabaseRoleError("Use the separately provisioned, restricted financial login")


class FinancialDatabase(Database):
    def __init__(self, settings: Settings) -> None:
        if (
            settings.financial_database_url is None
            or settings.financial_database_url == settings.database_url
        ):
            raise ValueError("Configure a separate FINANCIAL_DATABASE_URL")
        super().__init__(
            settings.model_copy(update={"database_url": settings.financial_database_url})
        )

    async def ping(self) -> None:
        async with self.session() as session:
            await verify_financial_role(session)


async def lock_financial_tenant(
    session: AsyncSession, tenant_id: UUID, *, wait: bool = True
) -> bool:
    """Shared transaction mutex, including writers without a control-plane connection.

    Control-plane writers lock tenant authority first, then this mutex. Background
    expiry takes only this mutex and must never acquire control-plane row locks.
    """
    function = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    acquired = await session.scalar(
        text(f"SELECT {function}(hashtextextended(:key, 0))"),
        {"key": f"financial-tenant:{tenant_id}"},
    )
    return wait or acquired is True


@asynccontextmanager
async def financial_transaction(
    sessions: async_sessionmaker[AsyncSession] | None,
) -> AsyncGenerator[AsyncSession]:
    if sessions is None:
        raise FinancialUnavailableError()
    async with sessions.begin() as session:
        await verify_financial_role(session)
        yield session
