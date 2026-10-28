"""Keep what each reply cost and how it ended. Numbers and names only."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.engine import Done
from app.ai.models import AiTurn
from app.ai.session import Session
from app.core.database.rls import set_tenant_scope

logger = logging.getLogger(__name__)


async def record_turn(
    sessions: async_sessionmaker[AsyncSession],
    session: Session,
    done: Done,
    *,
    first_audio_ms: int | None = None,
    interrupted: bool = False,
) -> None:
    """Best effort: a lost usage row must never cost the person their reply."""
    try:
        async with sessions.begin() as database:
            await set_tenant_scope(database, session.tenant_id)
            database.add(
                AiTurn(
                    tenant_id=session.tenant_id,
                    channel=session.channel,
                    call_id=session.conversation_id if session.channel == "voice" else None,
                    conversation_id=None if session.channel == "voice" else session.conversation_id,
                    model=done.model[:100],
                    input_tokens=done.usage.input_tokens,
                    output_tokens=done.usage.output_tokens,
                    first_text_ms=done.first_text_ms,
                    first_audio_ms=first_audio_ms,
                    interrupted=interrupted,
                    total_ms=done.total_ms,
                    steps=done.steps,
                    tools=list(done.tools),
                    outcome=done.outcome,
                    error=done.error[:40] if done.error else None,
                    cached_tokens=done.usage.cached_tokens,
                    cost_usd=round(done.usage.cost_usd, 6),
                    checks_cost_usd=round(done.checks.cost_usd, 6),
                    served_by=done.served_by[:60],
                )
            )
    except Exception:
        logger.warning("assistant_usage_not_recorded")
