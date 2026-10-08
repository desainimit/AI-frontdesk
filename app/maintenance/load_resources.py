"""Best-effort host-side resource sampling for the Phase 18 call driver."""

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Settings

_UNITS = {
    "B": 1,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
}


def _bytes(value: str) -> int | None:
    found = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?i?B)\s*", value)
    if found is None or found.group(2) not in _UNITS:
        return None
    return int(float(found.group(1)) * _UNITS[found.group(2)])


def _docker(container: str) -> tuple[float | None, int | None]:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
            capture_output=True,
            timeout=8,
            check=False,
            text=True,
        )
        if result.returncode:
            return None, None
        row = json.loads(result.stdout.strip().splitlines()[0])
        cpu = float(str(row["CPUPerc"]).strip().rstrip("%"))
        memory = _bytes(str(row["MemUsage"]).split("/")[0])
        return cpu, memory
    except (OSError, ValueError, IndexError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None, None


async def _db_connections(settings: Settings) -> int | None:
    owner = make_url(settings.migration_url)
    runtime = make_url(settings.database_url.get_secret_value())
    if not owner.username or not runtime.username or not owner.database:
        return None
    host = "localhost" if owner.host == "host.docker.internal" else owner.host
    engine = create_async_engine(
        owner.set(host=host),
        poolclass=NullPool,
        connect_args={"timeout": 3, "command_timeout": 3},
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = :database AND usename = :username"
                ),
                {"database": owner.database, "username": runtime.username},
            )
        return int(count) if count is not None else None
    except (OSError, SQLAlchemyError, TimeoutError, ValueError):
        return None
    finally:
        await engine.dispose()


async def _redis(settings: Settings) -> tuple[int | None, int | None]:
    raw = settings.redis_url.get_secret_value()
    parsed = urlsplit(raw)
    if parsed.hostname == "redis":
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        hostport = "localhost" + (f":{parsed.port}" if parsed.port is not None else "")
        raw = urlunsplit(
            (parsed.scheme, userinfo + hostport, parsed.path, parsed.query, parsed.fragment)
        )
    client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        raw, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        clients = cast(dict[str, object], await client.info("clients"))  # pyright: ignore[reportUnknownMemberType]
        memory = cast(dict[str, object], await client.info("memory"))  # pyright: ignore[reportUnknownMemberType]
        return int(cast(int, clients["connected_clients"])), int(cast(int, memory["used_memory"]))
    except (OSError, RedisError, ValueError, KeyError):
        return None, None
    finally:
        await client.aclose(close_connection_pool=True)


@dataclass
class ResourceSamples:
    cpu_percent: list[float] = field(default_factory=list[float])
    memory_bytes: list[int] = field(default_factory=list[int])
    db_runtime_connections: list[int] = field(default_factory=list[int])
    redis_clients: list[int] = field(default_factory=list[int])
    redis_memory_bytes: list[int] = field(default_factory=list[int])

    def summary(self) -> dict[str, float | int | None]:
        return {
            "api_cpu_percent_peak": max(self.cpu_percent, default=None),
            "api_memory_bytes_peak": max(self.memory_bytes, default=None),
            "db_runtime_connections_peak": max(self.db_runtime_connections, default=None),
            "redis_clients_peak": max(self.redis_clients, default=None),
            "redis_memory_bytes_peak": max(self.redis_memory_bytes, default=None),
            "sample_count": max(
                map(
                    len,
                    (
                        self.cpu_percent,
                        self.memory_bytes,
                        self.db_runtime_connections,
                        self.redis_clients,
                        self.redis_memory_bytes,
                    ),
                ),
                default=0,
            ),
        }


async def monitor(settings: Settings, container: str, stop: asyncio.Event) -> ResourceSamples:
    samples = ResourceSamples()
    while not stop.is_set():
        docker, db, redis = await asyncio.gather(
            asyncio.to_thread(_docker, container), _db_connections(settings), _redis(settings)
        )
        cpu, memory = docker
        clients, redis_memory = redis
        if cpu is not None:
            samples.cpu_percent.append(cpu)
        if memory is not None:
            samples.memory_bytes.append(memory)
        if db is not None:
            samples.db_runtime_connections.append(db)
        if clients is not None:
            samples.redis_clients.append(clients)
        if redis_memory is not None:
            samples.redis_memory_bytes.append(redis_memory)
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except TimeoutError:
            continue
    return samples
