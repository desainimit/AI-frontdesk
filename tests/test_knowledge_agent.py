import json
from uuid import uuid4

from pydantic import BaseModel

from app.agents.runtime import AgentRuntime
from app.agents.tools import ReadTool, ToolRegistry
from app.knowledge.schemas import KnowledgeQuery, KnowledgeResult, SourceView
from tests.test_agent_runtime import ScriptedProvider, context_and_state, decision


async def test_agent_answers_from_approved_source_metadata() -> None:
    context, state = context_and_state()

    async def read(_context: object, _payload: BaseModel) -> BaseModel:
        return KnowledgeResult(
            status="found",
            sources=[
                SourceView(
                    document_id=uuid4(),
                    version=1,
                    document_name="Parking guide.md",
                    section="Parking",
                    subsection=None,
                    page_number=2,
                    text="Parking is free behind the building.",
                    distance=0.01,
                )
            ],
        )

    provider = ScriptedProvider(
        [
            decision("get_knowledge", json.dumps({"question": "Where can I park?"})),
            {
                "intent": "clinic_inquiry",
                "response": "Parking is free behind the building (Parking guide.md, page 2).",
                "tool_calls": [],
            },
        ]
    )
    registry = ToolRegistry([ReadTool("get_knowledge", "Approved facts", KnowledgeQuery, read)])
    result = await AgentRuntime(provider, registry).respond(context, state, "Where can I park?")
    assert result.status == "completed" and result.tools_used == ["get_knowledge"]
    assert "Parking guide.md" in result.response
    final_prompt = provider.requests[1].messages[-1].content
    assert "Parking is free behind the building." in final_prompt
    assert "tenant_id" not in final_prompt


async def test_agent_no_source_cannot_hallucinate_answer() -> None:
    context, state = context_and_state()

    async def read(_context: object, _payload: BaseModel) -> BaseModel:
        return KnowledgeResult(status="not_found")

    provider = ScriptedProvider(
        [decision("get_knowledge", json.dumps({"question": "What are your parking fees?"}))]
    )
    registry = ToolRegistry([ReadTool("get_knowledge", "Approved facts", KnowledgeQuery, read)])
    result = await AgentRuntime(provider, registry).respond(context, state, "Parking fees?")
    assert result.status == "completed" and "approved information" in result.response
    assert len(provider.requests) == 1
