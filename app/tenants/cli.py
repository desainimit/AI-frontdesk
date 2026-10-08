"""Explicit operator entrypoint; privileged connections are never HTTP dependencies."""

import argparse
import asyncio
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Settings
from app.errors import ApplicationError
from app.tenants.bootstrap import BootstrapTenant, TenantBootstrapService


async def run(args: argparse.Namespace) -> None:
    settings = Settings.from_environment()
    if settings.migration_database_url is None:
        raise ValueError("MIGRATION_DATABASE_URL is required for operator commands")
    engine = create_async_engine(settings.migration_url, poolclass=NullPool, hide_parameters=True)
    try:
        service = TenantBootstrapService(async_sessionmaker(engine, expire_on_commit=False))
        if args.command == "create":
            result = await service.create(
                BootstrapTenant(
                    tenant_id=args.tenant_id,
                    name=args.name,
                    email=args.email,
                    display_name=args.display_name,
                    credential_days=args.credential_days,
                )
            )
            # Intentional one-time credential delivery to the invoking operator, never a log event.
            print(
                f"Tenant: {result.tenant_id}\nUser: {result.user_id}\n"
                f"Credential: {result.credential_id}"
            )
            print(f"Bearer token (store securely; shown once): {result.token.get_secret_value()}")
        else:
            await service.revoke(args.tenant_id, args.credential_id)
            print("Credential revoked.")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Privileged tenant bootstrap and credential revocation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--tenant-id", type=UUID, default=None, required=False)
    create.add_argument("--name", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--credential-days", type=int, default=30)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--tenant-id", type=UUID, required=True)
    revoke.add_argument("--credential-id", type=UUID, required=True)
    try:
        asyncio.run(run(parser.parse_args()))
    except (ApplicationError, SQLAlchemyError, ValidationError, ValueError):
        raise SystemExit(
            "Operation failed. Verify inputs, migration credentials, and unique tenant ID."
        ) from None


if __name__ == "__main__":
    main()
