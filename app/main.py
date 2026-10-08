import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from opentelemetry.metrics import Observation
from sqlalchemy.pool import QueuePool

from app.agents.factory import build_llm_provider
from app.api.domain import router as domain_router
from app.api.errors import register_error_handlers
from app.api.health import router as health_router
from app.api.human_auth import router as human_auth_router
from app.api.identity_auth import router as identity_auth_router
from app.api.integrations import router as integrations_router
from app.api.knowledge import router as knowledge_router
from app.api.middleware import RequestTelemetryMiddleware
from app.api.platform import router as platform_router
from app.api.postcall import router as postcall_router
from app.api.saas import router as saas_router
from app.api.telephony import router as telephony_router
from app.api.tenancy import router as tenancy_router
from app.cache.redis import RedisClient
from app.config.logging import configure_logging
from app.config.settings import Environment, Settings
from app.conversation.factory import build_conversation_store
from app.database.session import Database
from app.integrations.resolver import TenantIntegrationResolver
from app.observability.tracing import Telemetry
from app.resources import Dependency, Resources
from app.voice.circuit import CircuitRegistry
from app.voice.factory import build_stt_provider, build_tts_provider

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
            app.state.conversation_store = (
                build_conversation_store(config, redis) if config.telephony_enabled else None
            )
            # One resolver per process: tenant credentials are looked up per call or
            # request and never copied into process-wide settings.
            app.state.integration_resolver = (
                TenantIntegrationResolver(database.session_factory, config)
                if isinstance(database, Database)
                else None
            )
            app.state.voice_circuits = CircuitRegistry()
            app.state.stt_provider = None
            app.state.tts_provider = None
            # Platform-account providers serve legacy tenants only. With tenant
            # connections enabled they exist only if the platform keys are configured.
            platform_voice = not config.tenant_integrations_enabled or bool(config.legacy_tenants)
            if config.telephony_enabled and config.telephony_mode == "pipeline" and platform_voice:
                app.state.stt_provider = build_stt_provider(config, telemetry)
                app.state.tts_provider = build_tts_provider(config, telemetry)
            app.state.llm_provider = None
            if config.agent_enabled and (
                config.llm_provider == "mock" or config.platform_llm_key.get_secret_value()
            ):
                provider = build_llm_provider(config)
                app.state.llm_provider = provider
                stack.push_async_callback(provider.close)
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
    app.include_router(human_auth_router)
    app.include_router(identity_auth_router)
    app.include_router(integrations_router)
    app.include_router(postcall_router)
    app.include_router(telephony_router)
    app.include_router(saas_router)
    return app
