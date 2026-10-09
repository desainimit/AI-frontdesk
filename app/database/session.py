from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.database.rls import verify_runtime_role

POOL_TIMEOUT_SECONDS = 5
POOL_RECYCLE_SECONDS = 1800
COMMAND_TIMEOUT_SECONDS = 10


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=POOL_TIMEOUT_SECONDS,
            pool_recycle=POOL_RECYCLE_SECONDS,
            pool_use_lifo=True,
            hide_parameters=True,
            connect_args={
                "timeout": settings.dependency_timeout_seconds,
                "command_timeout": COMMAND_TIMEOUT_SECONDS,
                "server_settings": {"timezone": "UTC"},
            },
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """Each caller owns a fresh session; mutations must explicitly own a transaction."""
        async with self.session_factory() as session:
            yield session

    async def ping(self) -> None:
        async with self.session() as session:
            await session.execute(text("SELECT 1"))
            await verify_runtime_role(session)

    async def close(self) -> None:
        await self.engine.dispose()
