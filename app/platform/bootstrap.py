"""Environment-driven operator bootstrap; never import credentials into an Alembic revision."""

import asyncio
from uuid import uuid4

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.settings import Settings
from app.platform.models import PlatformAuditEvent, PlatformUser
from app.platform.passwords import hash_password
from app.platform.schemas import SuperAdminSeed


class BootstrapSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    super_admin_email: str = ""
    super_admin_password: SecretStr = SecretStr("")
    super_admin_name: str = "Super Admin"

    def command(self) -> SuperAdminSeed | None:
        if not self.super_admin_email and not self.super_admin_password.get_secret_value():
            return None
        return SuperAdminSeed(
            email=self.super_admin_email,
            password=self.super_admin_password,
            display_name=self.super_admin_name,
        )


async def seed_super_admin(
    sessions: async_sessionmaker[AsyncSession], command: SuperAdminSeed
) -> bool:
    async with sessions.begin() as session:
        # Serialize parallel deployment jobs so exactly one bootstrap audit is written.
        await session.execute(text("SELECT pg_advisory_xact_lock(724318902)"))
        existing = await session.scalar(
            select(PlatformUser.id).where(PlatformUser.email == command.email)
        )
        if existing is not None:
            return False  # Never rotate passwords, promote users, or re-enable disabled accounts.
        user = PlatformUser(
            email=command.email,
            display_name=command.display_name,
            password_hash=await asyncio.to_thread(
                hash_password, command.password.get_secret_value()
            ),
            role="super_admin",
        )
        session.add(user)
        await session.flush()
        session.add(
            PlatformAuditEvent(user_id=user.id, action="super_admin_created", request_id=uuid4())
        )
        return True


async def bootstrap(settings: Settings, command: SuperAdminSeed) -> bool:
    if settings.migration_database_url is None:
        raise ValueError("Migration credentials required")
    engine = create_async_engine(settings.migration_url, poolclass=NullPool, hide_parameters=True)
    try:
        return await seed_super_admin(async_sessionmaker(engine, expire_on_commit=False), command)
    finally:
        await engine.dispose()


def main() -> None:
    try:
        command = BootstrapSettings().command()
        if command is None:
            print("Super admin bootstrap skipped: credentials are not configured.")
            return
        created = asyncio.run(bootstrap(Settings.from_environment(), command))
    except (ValidationError, ValueError, SQLAlchemyError):
        raise SystemExit(
            "Super admin bootstrap failed. Set SUPER_ADMIN_EMAIL and a 8-256 character "
            "SUPER_ADMIN_PASSWORD; verify migrations and owner credentials. "
            "Existing accounts are never changed."
        ) from None
    print("Super admin created." if created else "Super admin already exists; unchanged.")


if __name__ == "__main__":
    main()
