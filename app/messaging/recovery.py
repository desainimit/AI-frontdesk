"""Find text conversations whose reply was lost and answer them."""

import asyncio
import logging
from uuid import UUID

from app.ai.runtime import AssistantRuntime
from app.messaging.service import TextAssistant

logger = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 20
# People often send two or three short texts in a row; they are answered as one.
SETTLE_SECONDS = 2.0


async def answer_soon(runtime: AssistantRuntime, tenant_id: UUID, conversation_id: UUID) -> None:
    """Answer a thread shortly after its message arrived, and again while more keep coming."""
    await asyncio.sleep(SETTLE_SECONDS)
    assistant = TextAssistant(runtime)
    try:
        for _ in range(3):
            if not await assistant.answer(tenant_id, conversation_id):
                return
    except Exception:
        # The recovery loop below picks the thread up again.
        logger.warning("text_turn_not_started", extra={"conversation_id": str(conversation_id)})


async def answer_lost_texts(runtime: AssistantRuntime) -> None:
    """Runs for the life of the API process. A turn normally starts as its message
    arrives; this catches the ones a restart or a crash left unanswered."""
    assistant = TextAssistant(runtime)
    while True:
        await asyncio.sleep(CHECK_EVERY_SECONDS)
        try:
            for tenant_id, conversation_id in await assistant.backlog():
                await assistant.answer(tenant_id, conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("lost_text_recovery_failed")
