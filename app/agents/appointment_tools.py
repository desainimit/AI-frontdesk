from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.appointment_flow import (
    AppointmentFlow,
    AvailabilityInput,
    BookInput,
    CancelInput,
    RescheduleInput,
)
from app.agents.context import AgentContext
from app.agents.tools import ReadTool, StatefulTool, ToolRegistry, directory_tools
from app.auth.context import ActorType, RequestContext
from app.calls.access import CallPatientAccess, IdentifyPatient
from app.calls.directory import CallDirectoryService, DirectoryQuery
from app.conversation.state import ConversationState
from app.core.database.unit_of_work import UnitOfWork
from app.core.settings import Settings
from app.integrations.resolver import TenantIntegrationResolver
from app.knowledge.embeddings import open_embedder
from app.knowledge.schemas import KnowledgeQuery
from app.knowledge.service import KnowledgeService
from app.knowledge.storage import build_storage


def appointment_tools(
    sessions: async_sessionmaker[AsyncSession],
    timeout_seconds: float = 3,
    knowledge_settings: Settings | None = None,
    resolver: TenantIntegrationResolver | None = None,
) -> tuple[ToolRegistry, AppointmentFlow]:
    directory = CallDirectoryService(sessions)
    flow = AppointmentFlow(UnitOfWork(sessions), CallPatientAccess(sessions))

    async def providers(context: AgentContext, payload: BaseModel) -> BaseModel:
        return await directory.read(
            context.call, "providers", DirectoryQuery.model_validate(payload.model_dump())
        )

    async def book(
        context: AgentContext, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        return await flow.propose(context, state, "book_appointment", payload)

    async def cancel(
        context: AgentContext, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        return await flow.propose(context, state, "cancel_appointment", payload)

    async def reschedule(
        context: AgentContext, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        return await flow.propose(context, state, "reschedule_appointment", payload)

    tools = directory_tools(directory).registered_tools()
    tools.extend(
        [
            ReadTool(
                "get_providers", "List clinic doctors and location IDs.", DirectoryQuery, providers
            ),
            StatefulTool(
                "identify_patient",
                "Redeem a reception-issued call access code. Never guess codes.",
                IdentifyPatient,
                flow.identify,
            ),
            StatefulTool(
                "check_availability",
                "Read up to 20 available slots in a timezone-aware range of at most 31 days.",
                AvailabilityInput,
                flow.slots,
            ),
            StatefulTool(
                "list_my_appointments",
                "List only the authorized patient's appointments.",
                DirectoryQuery,
                flow.list_appointments,
            ),
            StatefulTool(
                "book_appointment",
                "Propose a booking; the server requests explicit confirmation before writing.",
                BookInput,
                book,
            ),
            StatefulTool(
                "cancel_appointment",
                "Propose cancellation of the authorized patient's appointment.",
                CancelInput,
                cancel,
            ),
            StatefulTool(
                "reschedule_appointment",
                "Propose a new time for the authorized patient's appointment.",
                RescheduleInput,
                reschedule,
            ),
        ]
    )
    if knowledge_settings is not None:

        async def get_knowledge(context: AgentContext, payload: BaseModel) -> BaseModel:
            query = KnowledgeQuery.model_validate(payload.model_dump())
            embedder = await open_embedder(knowledge_settings, resolver, context.call.tenant_id)
            try:
                service = KnowledgeService(
                    UnitOfWork(sessions), build_storage(knowledge_settings), embedder
                )
                caller = RequestContext(
                    uuid4(),
                    context.call.tenant_id,
                    None,
                    ActorType.CALLER,
                    call_id=context.call.call_id,
                    provider_call_id=context.call.provider_call_id,
                )
                return await service.search(caller, query.question, query.limit)
            finally:
                await embedder.close()

        tools.append(
            ReadTool(
                "get_knowledge",
                "Find approved static business facts with source pages. Never use for live "
                "availability, patient records, payment, or schedules.",
                KnowledgeQuery,
                get_knowledge,
            )
        )
    return ToolRegistry(tools, timeout_seconds), flow
