"""Rehearse the real forward cutover against isolated legacy PostgreSQL tables."""

from importlib import import_module
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text

from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


def apply_cutover(connection: Connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        import_module("migrations.versions.0024_global_human_identity").upgrade()
        import_module("migrations.versions.0025_erase_superseded_reset_mail").upgrade()


async def test_legacy_identity_cutover_and_reset_mail_erasure(tenancy: TenancyFixture) -> None:
    # This schema and all fixtures roll back together; public application data is untouched.
    schema = "identity_cutover_" + uuid4().hex
    async with tenancy.admin() as session:
        await session.execute(text(f'CREATE SCHEMA "{schema}"'))
        await session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        statements = (
            "CREATE TABLE users (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "email text NOT NULL)",
            "CREATE TABLE human_accounts (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "user_id uuid NOT NULL, password_hash text NOT NULL)",
            "CREATE TABLE human_sessions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "user_id uuid NOT NULL)",
            "CREATE TABLE auth_challenges (id uuid PRIMARY KEY, purpose text NOT NULL, "
            "consumed_at timestamptz)",
            "CREATE TABLE auth_mail (id uuid PRIMARY KEY, challenge_id uuid NOT NULL, "
            "status text NOT NULL, ciphertext bytea)",
        )
        for statement in statements:
            await session.execute(text(statement))
        fixtures = (
            ("single@example.test", "single-legacy-hash"),
            ("shared@example.test", "identical-legacy-hash"),
            ("SHARED@example.test", "identical-legacy-hash"),
            ("conflict@example.test", "first-salted-hash"),
            ("CONFLICT@example.test", "second-salted-hash"),
        )
        for email, password_hash in fixtures:
            user, tenant = uuid4(), uuid4()
            await session.execute(
                text("INSERT INTO users VALUES (:user, :tenant, :email)"),
                {"user": user, "tenant": tenant, "email": email},
            )
            await session.execute(
                text("INSERT INTO human_accounts VALUES (:id, :tenant, :user, :password_hash)"),
                {"id": uuid4(), "tenant": tenant, "user": user, "password_hash": password_hash},
            )
            await session.execute(
                text("INSERT INTO human_sessions VALUES (:id, :tenant, :user)"),
                {"id": uuid4(), "tenant": tenant, "user": user},
            )
        reset, invitation = uuid4(), uuid4()
        for challenge, purpose in ((reset, "reset"), (invitation, "invitation")):
            await session.execute(
                text("INSERT INTO auth_challenges VALUES (:id, :purpose, NULL)"),
                {"id": challenge, "purpose": purpose},
            )
            await session.execute(
                text("INSERT INTO auth_mail VALUES (:id, :challenge, 'queued', :payload)"),
                {"id": uuid4(), "challenge": challenge, "payload": b"legacy-secret"},
            )
        await (await session.connection()).run_sync(apply_cutover)
        identities: dict[str, str | None] = {
            row.email: row.password_hash
            for row in await session.execute(
                text("SELECT email, password_hash FROM human_identities")
            )
        }
        assert identities == {
            "single@example.test": "single-legacy-hash",
            "shared@example.test": "identical-legacy-hash",
            "conflict@example.test": None,
        }
        assert await session.scalar(
            text(
                "SELECT count(*) FROM human_sessions s JOIN human_accounts h "
                "ON s.identity_id = h.identity_id AND s.tenant_id = h.tenant_id "
                "AND s.user_id = h.user_id WHERE s.credential_revision = 1"
            )
        ) == len(fixtures)
        assert not await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = 'human_accounts' "
                "AND column_name = 'password_hash')"
            ),
            {"schema": schema},
        )
        reset_row = (
            await session.execute(
                text(
                    "SELECT c.consumed_at, m.status, m.ciphertext FROM auth_challenges c "
                    "JOIN auth_mail m ON m.challenge_id = c.id WHERE c.id = :id"
                ),
                {"id": reset},
            )
        ).one()
        assert reset_row.consumed_at is not None
        assert reset_row.status == "cancelled" and reset_row.ciphertext is None
        invite_row = (
            await session.execute(
                text(
                    "SELECT c.consumed_at, m.status, m.ciphertext FROM auth_challenges c "
                    "JOIN auth_mail m ON m.challenge_id = c.id WHERE c.id = :id"
                ),
                {"id": invitation},
            )
        ).one()
        assert invite_row.consumed_at is None
        assert invite_row.status == "queued" and invite_row.ciphertext == b"legacy-secret"
        assert await session.scalar(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                "WHERE oid = 'human_identities'::regclass"
            )
        )
