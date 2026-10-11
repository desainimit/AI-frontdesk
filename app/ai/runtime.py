"""Build the receptionist for one conversation: the clinic's model account or the
platform's, with the safeguards around it."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.engine import Engine
from app.ai.llm.base import LLMProvider
from app.ai.llm.factory import build_llm_provider, llm_from_connection
from app.ai.llm.gateway import ModelGateway, TurnLimits, breaker_for
from app.ai.tools.toolbox import build_tools
from app.core.settings import Settings
from app.integrations.resolver import IntegrationUnavailableError, TenantIntegrationResolver
from app.safety.classifier import LLMSafetyClassifier
from app.safety.engine import SafetyEngine
from app.voice.circuit import CircuitRegistry


@dataclass(frozen=True)
class Models:
    main: LLMProvider
    # The quicker model: the safety check, and one last attempt at a reply.
    light: LLMProvider | None
    # The ones opened for this conversation, to be closed when it ends.
    owned: tuple[LLMProvider, ...]
    # Which model account is in use, for its circuit breaker.
    account: str

    async def close(self) -> None:
        for model in self.owned:
            with suppress(Exception):
                await model.close()


async def models_for(
    settings: Settings,
    resolver: TenantIntegrationResolver | None,
    tenant_id: UUID,
    substitute: LLMProvider | None = None,
) -> Models:
    """The clinic's own model connection when it has one, otherwise the platform's.

    With neither, the conversation fails before anyone is told anything; nothing falls
    back to another clinic's key.
    """
    connection = await resolver.active(tenant_id, "llm") if resolver is not None else None
    if connection is not None:
        key, config = connection.secret("api_key"), dict(connection.config)
        main = llm_from_connection(connection.provider, key, config)
        light = llm_from_connection(connection.provider, key, config, light=True)
        return Models(main, light, (main, light), str(connection.integration_id))
    if substitute is not None:
        return Models(substitute, None, (), "platform")
    if not settings.platform_llm_key.get_secret_value():
        raise IntegrationUnavailableError()
    main, light = build_llm_provider(settings), build_llm_provider(settings, light=True)
    return Models(main, light, (main, light), "platform")


class AssistantRuntime:
    """What every conversation in this process shares."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        resolver: TenantIntegrationResolver | None,
        circuits: CircuitRegistry,
        limits: TurnLimits,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.resolver = resolver
        self.circuits = circuits
        self.limits = limits
        # Empty in production. A test sets it to substitute its own model.
        self.substitute: LLMProvider | None = None

    def engine(self, models: Models, tenant_id: UUID, *, transfer_available: bool) -> Engine:
        registry, flow, identity = build_tools(self.sessions, self.settings, self.resolver)
        return Engine(
            ModelGateway(
                models.main,
                fallback=models.light,
                breaker=breaker_for(self.circuits, tenant_id, models.account),
                slot=self.limits.slot(tenant_id),
            ),
            registry,
            flow,
            # The model's own reading of the risk uses the quicker model and runs beside
            # the reply; the rules always run first.
            SafetyEngine(
                LLMSafetyClassifier(models.light or models.main)
                if self.settings.safety_semantic_enabled
                else None,
                transfer_available=transfer_available,
            ),
            identity,
        )

    async def ensure_model(self, tenant_id: UUID) -> None:
        """Fail now, with a reason that can be shown, when no model account is set up."""
        await (await models_for(self.settings, self.resolver, tenant_id, self.substitute)).close()

    @asynccontextmanager
    async def open(
        self, tenant_id: UUID, *, transfer_available: bool = False
    ) -> AsyncGenerator[Engine]:
        models = await models_for(self.settings, self.resolver, tenant_id, self.substitute)
        try:
            yield self.engine(models, tenant_id, transfer_available=transfer_available)
        finally:
            await models.close()
