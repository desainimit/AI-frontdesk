"""Is the product working? One page for a platform administrator.

The worker looks at its own queues once a minute and writes down what it sees; that
row is also the proof the worker is running. The API adds what only it can see: how
the receptionist's replies are going and whether any text message is still unanswered.

Each line says what is wrong in words and how serious it is. The thresholds here are the
ones to alert on; see docs/runbook.md.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.store import verify_worker_role
from app.identity.sessions import platform_scope
from app.platform.models import PlatformStatus
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission

logger = logging.getLogger(__name__)

Level = Literal["ok", "warning", "problem"]
WORKER_EVERY_SECONDS = 60
# The worker writes every minute; three missed writes mean it is not running.
WORKER_SILENT_AFTER = timedelta(minutes=3)

QUEUES = text("""
SELECT
  (SELECT count(*) FROM outbound_messages
    WHERE status = 'queued' AND available_at < now() - interval '5 minutes') AS messages_late,
  (SELECT count(*) FROM outbound_messages
    WHERE status = 'failed' AND updated_at > now() - interval '24 hours') AS messages_failed,
  (SELECT count(*) FROM auth_mail
    WHERE status = 'queued' AND available_at < now() - interval '5 minutes') AS mail_late,
  (SELECT count(*) FROM auth_mail WHERE status = 'failed') AS mail_failed,
  (SELECT count(*) FROM knowledge_jobs
    WHERE status = 'pending' AND available_at < now() - interval '10 minutes') AS documents_late,
  (SELECT count(*) FROM workflow_intents
    WHERE status = 'pending' AND available_at < now() - interval '10 minutes') AS reports_late,
  (SELECT count(*) FROM workflow_intents WHERE status = 'dead') AS reports_failed,
  (SELECT count(*) FROM outbox_events
    WHERE status = 'pending' AND available_at < now() - interval '10 minutes')
    AS calendar_writes_late,
  (SELECT count(*) FROM outbox_events WHERE status = 'dead') AS calendar_writes_failed,
  (SELECT count(*) FROM calendar_connections WHERE status = 'needs_attention')
    AS calendars_disconnected,
  (SELECT count(*) FROM calendar_connections
    WHERE status = 'connected'
      AND coalesce(last_synced_at, created_at) < now() - interval '30 minutes')
    AS calendars_stale
""")

RECEPTIONIST = text("""
SELECT count(*) AS replies,
       count(*) FILTER (WHERE outcome IN ('unavailable', 'blocked', 'interrupted')) AS failed,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY first_text_ms) AS first_words_ms,
       mode() WITHIN GROUP (ORDER BY error) AS usual_error
FROM ai_turns
WHERE created_at > now() - interval '1 hour' AND channel <> 'test'
""")

UNANSWERED = text("""
SELECT count(*) FROM conversations
WHERE pending_since < now() - interval '2 minutes' AND handler = 'assistant'
""")


# The most common reason for failed replies, as the start of a sentence.
REASONS = {
    "llm_credentials_rejected": "The AI account's key is being refused: save a working key ",
    "llm_credits_exhausted": "The AI account has run out of credit: top it up, then check it ",
    "llm_rate_limited": "The AI account is hitting its rate limit: raise it, or check the account ",
    "llm_request_rejected": "The AI service refuses the requests; check the chosen model ",
}


class HealthItem(BaseModel):
    key: str
    title: str
    status: Level
    detail: str


class SystemHealth(BaseModel):
    status: Level
    items: list[HealthItem]
    checked_at: datetime


async def record_worker_status(sessions: async_sessionmaker[AsyncSession]) -> bool:
    """Write down what the worker's queues look like. Runs in the worker, once a minute."""
    async with sessions.begin() as session:
        await verify_worker_role(session)
        queues = dict((await session.execute(QUEUES)).mappings().one())
        now = datetime.now(UTC)
        await session.execute(
            insert(PlatformStatus)
            .values(id=1, worker_seen_at=now, queues=queues)
            .on_conflict_do_update(
                index_elements=["id"], set_={"worker_seen_at": now, "queues": queues}
            )
        )
    late = {name: count for name, count in queues.items() if count and name.endswith("_late")}
    if late:
        # One line an alert can be set on, with numbers only.
        logger.error("queues_running_late", extra={"queues": ",".join(sorted(late))})
    return False


def _count(value: int, one: str, many: str) -> str:
    return f"{value} {one if value == 1 else many}"


def _queue_items(queues: dict[str, Any]) -> list[HealthItem]:
    def number(name: str) -> int:
        return int(queues.get(name) or 0)

    def item(
        key: str, title: str, late: int, failed: int, late_text: str, failed_text: str
    ) -> HealthItem:
        if late:
            return HealthItem(key=key, title=title, status="problem", detail=late_text)
        if failed:
            return HealthItem(key=key, title=title, status="warning", detail=failed_text)
        return HealthItem(key=key, title=title, status="ok", detail="Nothing is waiting.")

    late, failed = number("messages_late"), number("messages_failed")
    items = [
        item(
            "messages",
            "Messages to patients",
            late,
            failed,
            f"{_count(late, 'message has', 'messages have')} been waiting more than 5 minutes.",
            f"{_count(failed, 'message', 'messages')} could not be sent in the last 24 hours. "
            "The delivery log says why.",
        )
    ]
    late, failed = number("mail_late"), number("mail_failed")
    items.append(
        item(
            "account_email",
            "Invitations and password emails",
            late,
            failed,
            f"{_count(late, 'email has', 'emails have')} been waiting more than 5 minutes. "
            "Check the email server.",
            f"{_count(failed, 'email', 'emails')} could not be sent. Check the email server.",
        )
    )
    late = number("documents_late")
    items.append(
        item(
            "documents",
            "Knowledge documents",
            late,
            0,
            f"{_count(late, 'document has', 'documents have')} been waiting more than "
            "10 minutes to be read.",
            "",
        )
    )
    late, failed = number("reports_late"), number("reports_failed")
    items.append(
        item(
            "reports",
            "Call reports",
            late,
            failed,
            f"{_count(late, 'report has', 'reports have')} been waiting more than 10 minutes.",
            f"{_count(failed, 'report', 'reports')} could not be written.",
        )
    )
    late = number("calendar_writes_late")
    failed = number("calendar_writes_failed") + number("calendars_disconnected")
    stale = number("calendars_stale")
    items.append(
        item(
            "calendars",
            "Calendar sync",
            late + stale,
            failed,
            "Appointments are not reaching providers' calendars, or calendars have not been "
            "read for more than 30 minutes.",
            f"{_count(failed, 'calendar or appointment needs', 'calendars or appointments need')} "
            "attention. The provider's Calendar tab says which.",
        )
    )
    return items


class SystemHealthService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def read(self, context: PlatformContext) -> SystemHealth:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            status = await session.get(PlatformStatus, 1)
            replies = (await session.execute(RECEPTIONIST)).mappings().one()
            # Read across clinics for this one count; see the policy on the table.
            await session.execute(text("SELECT set_config('app.assistant_backlog', 'on', true)"))
            unanswered = int(await session.scalar(UNANSWERED) or 0)

        items: list[HealthItem] = []
        if status is None:
            items.append(
                HealthItem(
                    key="worker",
                    title="Background worker",
                    status="problem",
                    detail="The worker has never reported. Messages, reminders and calendar "
                    "sync do not run without it.",
                )
            )
        elif now - status.worker_seen_at > WORKER_SILENT_AFTER:
            minutes = int((now - status.worker_seen_at).total_seconds() // 60)
            items.append(
                HealthItem(
                    key="worker",
                    title="Background worker",
                    status="problem",
                    detail=f"The worker last reported {minutes} minutes ago. Messages, "
                    "reminders and calendar sync are not running.",
                )
            )
        else:
            items.append(
                HealthItem(key="worker", title="Background worker", status="ok", detail="Running.")
            )
            items.extend(_queue_items(status.queues))

        total, failed = int(replies["replies"]), int(replies["failed"])
        first = replies["first_words_ms"]
        if total >= 10 and failed / total > 0.1:
            receptionist = HealthItem(
                key="receptionist",
                title="The receptionist",
                status="problem",
                detail=f"{failed} of {total} replies failed in the last hour. "
                + REASONS.get(str(replies["usual_error"]), "Check the AI model account ")
                + "under Providers and email.",
            )
        elif first is not None and total >= 5 and float(first) > 2500:
            receptionist = HealthItem(
                key="receptionist",
                title="The receptionist",
                status="warning",
                detail=f"Replies are slow: {float(first) / 1000:.1f} seconds to the first "
                f"words, over {total} replies in the last hour.",
            )
        else:
            receptionist = HealthItem(
                key="receptionist",
                title="The receptionist",
                status="ok",
                detail="No replies in the last hour."
                if total == 0
                else f"{total} replies in the last hour, {failed} failed.",
            )
        items.append(receptionist)
        items.append(
            HealthItem(
                key="texts",
                title="Text messages from patients",
                status="problem" if unanswered else "ok",
                detail=f"{_count(unanswered, 'conversation has', 'conversations have')} waited "
                "more than 2 minutes for a reply."
                if unanswered
                else "Every message has been answered.",
            )
        )
        worst: Level = (
            "problem"
            if any(item.status == "problem" for item in items)
            else "warning"
            if any(item.status == "warning" for item in items)
            else "ok"
        )
        return SystemHealth(status=worst, items=items, checked_at=now)
