import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from opentelemetry.metrics import Observation
from sqlalchemy.pool import QueuePool

from app.ai.llm.gateway import TurnLimits
from app.ai.runtime import AssistantRuntime
from app.api.assistant import router as assistant_router
from app.api.auth import router as auth_router
from app.api.calendar import router as calendar_router
from app.api.domain import router as domain_router
from app.api.errors import register_error_handlers
from app.api.health import router as health_router
from app.api.integrations import router as integrations_router
from app.api.knowledge import router as knowledge_router
from app.api.middleware import RequestTelemetryMiddleware
from app.api.platform import router as platform_router
from app.api.postcall import router as postcall_router
from app.api.settings import router as settings_router
from app.api.telephony import router as telephony_router
from app.api.tenancy import router as tenancy_router
from app.conversation.factory import build_conversation_store
from app.core.database.session import Database
from app.core.logging import configure_logging
from app.core.redis import RedisClient
from app.core.resources import Dependency, Resources
from app.core.settings import Environment, Settings
from app.core.telemetry import Telemetry
from app.integrations.resolver import TenantIntegrationResolver
from app.messaging.recovery import answer_lost_texts
from app.platform.connections import keep_platform_fresh, refresh_platform
from app.voice.circuit import CircuitRegistry

logger = logging.getLogger(__name__)
ResourceFactory = Callable[[Settings], Dependency]


def create_app(
    settings: Settings | None = None,
    *,
    database_factory: ResourceFactory = Database,
    redis_factory: ResourceFactory = RedisClient,
    rate_limits: bool = True,
) -> FastAPI:
    config = settings if settings is not None else Settings.from_environment()
    configure_logging(config.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            telemetry = Telemetry(config)
            stack.push_async_callback(telemetry.close)
            app.state.telemetry = telemetry
            database = database_factory(config)
            stack.push_async_callback(database.close)
            if isinstance(database, Database) and isinstance(database.engine.pool, QueuePool):
                pool = database.engine.pool
                telemetry.meter.create_observable_gauge(
                    "db.pool.checked_out",
                    callbacks=[lambda _options: [Observation(pool.checkedout())]],
                    description="Checked-out application database connections",
                )
            redis = redis_factory(config)
            stack.push_async_callback(redis.close)
            app.state.resources = Resources(database=database, redis=redis)
            app.state.conversation_store = build_conversation_store(config, redis)
            # One resolver per process: tenant credentials are looked up per call or
            # request and never copied into process-wide settings.
            app.state.integration_resolver = (
                TenantIntegrationResolver(database.session_factory, config)
                if isinstance(database, Database)
                else None
            )
            app.state.voice_circuits = CircuitRegistry()
            # Speech providers are built per call from the clinic's connection or the
            # platform keys. These two stay empty in production; a test sets them to
            # substitute its own provider.
            app.state.stt_provider = None
            app.state.tts_provider = None
            # Empty in production: the platform model account is opened per call. A test
            # sets it to substitute its own model.
            app.state.llm_provider = None
            app.state.turn_limits = TurnLimits()
            # Replies to text messages being written right now.
            app.state.text_turns = set()
            if isinstance(database, Database):
                # Provider accounts and product settings come from the database. Other
                # API processes pick up a change within the refresh interval.
                await refresh_platform(config, database.session_factory)
                refresher = asyncio.create_task(
                    keep_platform_fresh(config, database.session_factory)
                )
                stack.callback(refresher.cancel)
                app.state.assistant = AssistantRuntime(
                    database.session_factory,
                    config,
                    app.state.integration_resolver,
                    app.state.voice_circuits,
                    app.state.turn_limits,
                )
                recovery = asyncio.create_task(answer_lost_texts(app.state.assistant))
                stack.callback(recovery.cancel)
            logger.info("application_started")
            try:
                yield
            finally:
                logger.info("application_stopping")
        logger.info("application_stopped")

    expose_docs = config.environment in {Environment.LOCAL, Environment.DEVELOPMENT}
    app = FastAPI(
        title="AI Frontdesk",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    app.state.settings = config
    # Always on. Only a test that substitutes Redis passes False; no setting can.
    app.state.rate_limits = rate_limits
    app.add_middleware(RequestTelemetryMiddleware)
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(tenancy_router)
    app.include_router(domain_router)
    app.include_router(knowledge_router)
    app.include_router(platform_router)
    app.include_router(auth_router)
    app.include_router(integrations_router)
    app.include_router(postcall_router)
    app.include_router(telephony_router)
    app.include_router(settings_router)
    app.include_router(calendar_router)
    app.include_router(assistant_router)
    return app
