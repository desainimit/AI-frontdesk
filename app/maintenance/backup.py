"""Operator backup and isolated restore verification for local PostgreSQL."""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config.settings import Settings


def _connection(settings: Settings) -> tuple[dict[str, str], str]:
    url = make_url(settings.migration_url)
    if not url.database or not url.username or not url.password or not url.host:
        raise ValueError("Backup requires a complete migration database URL")
    env = os.environ.copy()
    env.update(
        PGHOST=url.host,
        PGPORT=str(url.port or 5432),
        PGUSER=url.username,
        PGPASSWORD=url.password,
        PGCONNECT_TIMEOUT="10",
    )
    return env, url.database


def _run(args: list[str], env: dict[str, str]) -> bytes:
    try:
        result = subprocess.run(args, env=env, capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("PostgreSQL maintenance command unavailable") from None
    if result.returncode != 0:
        raise RuntimeError("PostgreSQL maintenance command failed")
    return result.stdout


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_backup(settings: Settings, directory: Path) -> Path:
    env, database = _connection(settings)
    root = directory.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"frontdesk-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}.dump"
    target = (root / name).resolve()
    temporary = (root / f".{name}.partial").resolve()
    if not target.is_relative_to(root) or not temporary.is_relative_to(root):
        raise ValueError("Backup path must remain inside the chosen directory")
    try:
        _run(["pg_dump", "--format=custom", "--file", str(temporary), database], env)
        _run(["pg_restore", "--list", str(temporary)], env)
        checksum = _digest(temporary)
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, target)
        manifest = root / f"{name}.json"
        manifest.write_text(
            json.dumps(
                {
                    "database": database,
                    "created_at": datetime.now(UTC).isoformat(),
                    "format": "pg_dump_custom",
                    "sha256": checksum,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            manifest.chmod(0o600)
        return target
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_restore(settings: Settings, backup: Path, target_database: str) -> str:
    if not re.fullmatch(r"frontdesk_restore_test_[a-z0-9_]{1,40}", target_database):
        raise ValueError("Restore target must be a new frontdesk_restore_test_* database")
    env, source_database = _connection(settings)
    if target_database == source_database:
        raise ValueError("Restore target must differ from the source database")
    archive = backup.resolve(strict=True)
    manifest = archive.with_name(archive.name + ".json")
    if not manifest.is_file():
        raise ValueError("Backup manifest is missing")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("sha256") != _digest(archive):
        raise ValueError("Backup checksum does not match the manifest")
    _run(["pg_restore", "--list", str(archive)], env)
    _run(["createdb", "--template=template0", target_database], env)
    _run(
        [
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-acl",
            "--dbname",
            target_database,
            str(archive),
        ],
        env,
    )
    version = (
        _run(
            [
                "psql",
                "--dbname",
                target_database,
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--command",
                "SELECT version_num FROM alembic_version",
            ],
            env,
        )
        .decode("utf-8")
        .strip()
    )
    if not version:
        raise RuntimeError("Restored database has no Alembic version")
    vector = (
        _run(
            [
                "psql",
                "--dbname",
                target_database,
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--command",
                "SELECT count(*) FROM pg_extension WHERE extname = 'vector'",
            ],
            env,
        )
        .decode("utf-8")
        .strip()
    )
    if vector != "1":
        raise RuntimeError("Restored database is missing the vector extension")
    protected = (
        _run(
            [
                "psql",
                "--dbname",
                target_database,
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--command",
                "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                "AND relname IN ('tenants','users','audit_events','calls','call_transcripts') "
                "AND relrowsecurity AND relforcerowsecurity",
            ],
            env,
        )
        .decode("utf-8")
        .strip()
    )
    if protected != "5":
        raise RuntimeError("Restored database has missing tenant row-level security")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup_command = commands.add_parser("backup")
    backup_command.add_argument("--directory", required=True, type=Path)
    restore_command = commands.add_parser("verify-restore")
    restore_command.add_argument("--backup", required=True, type=Path)
    restore_command.add_argument("--target-database", required=True)
    args = parser.parse_args()
    settings = Settings.from_environment()
    if args.command == "backup":
        result = create_backup(settings, args.directory)
        print(f"Backup created with checksum manifest: {result}")
    else:
        version = verify_restore(settings, args.backup, args.target_database)
        print(f"Isolated restore verified at Alembic revision {version}")


if __name__ == "__main__":
    main()
