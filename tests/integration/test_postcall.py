from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.calls.models import PhoneNumber
from app.calls.service import CallService
from app.conversation.manager import ConversationStateManager
from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.core.errors import NotFoundError
from app.notifications.models import OutboundMessage
from app.postcall.schemas import PostCallInput
from app.postcall.service import PostCallQueryService
from app.settings.schemas import SettingsSection
from app.settings.service import SettingsService
from app.tickets.service import TicketService, UpdateTicket
from app.workflows.activities import WorkflowActivities
from app.workflows.intent import WorkflowRequest
from app.workflows.models import WorkflowIntent
from app.workflows.runner import JobRunner
from app.workflows.store import WorkflowIntentStore
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_post_call_callback_race_opens_a_ticket_and_feeds_tenant_analytics(
    tenancy: TenancyFixture,
) -> None:
    assert isinstance(tenancy.settings.worker_database_url, SecretStr)
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
    calls = CallService(tenancy.database.session_factory)
    context = await calls.receive(number, "CA" + uuid4().hex, uuid4())
    # A provider callback can end the call before media cleanup saves the transcript.
    await calls.finish(context, "failed", "provider_failed")
    worker_settings = tenancy.settings.model_copy(
        update={"database_url": tenancy.settings.worker_database_url}
    )
    worker_db = Database(worker_settings)
    try:
        activities = WorkflowActivities(worker_db.session_factory)
        request = WorkflowRequest(str(context.tenant_id), str(context.call_id))
        assert await activities.record_post_call(request) == "pending"
        state = ConversationStateManager.create_initial_state(
            context.call_id, context.tenant_id, context.provider_call_id
        )
        ConversationStateManager.process_caller_utterance(state, "I need help at alice@example.com")
        state.append_transcript_turn("agent", "A staff member will follow up.")
        await calls.finish(
            context,
            "completed",
            "stream_stopped",
            post_call_input=PostCallInput.capture(state),
        )
        # The clinic asked for an email whenever something is passed to its team.
        settings = SettingsService(tenancy.database.session_factory)
        owner = tenancy.context(tenancy.a)
        notifications = (await settings.get(owner)).settings.notifications.model_dump(mode="json")
        notifications["recipients"] = ["frontdesk@clinic.test"]
        await settings.save(owner, SettingsSection.NOTIFICATIONS, notifications)
        assert await activities.record_post_call(request) == "recorded"
        assert await activities.record_post_call(request) == "recorded"
        query = PostCallQueryService(tenancy.database.session_factory)
        report = await query.report(tenancy.context(tenancy.a), context.call_id)
        assert report.outcome.call_status == "failed"
        assert report.outcome.follow_up == "human_review"
        assert "alice@example.com" not in report.summary
        with pytest.raises(NotFoundError):
            await query.report(tenancy.context(tenancy.b), context.call_id)
        # A call that needs a person becomes a ticket, opened by the worker.
        tickets = TicketService(tenancy.database.session_factory)
        waiting = await tickets.list(tenancy.context(tenancy.a), 10, 0)
        assert [(row.number, row.source, row.call_id) for row in waiting] == [
            (1, "call", context.call_id)
        ]
        assert await tickets.list(tenancy.context(tenancy.b), 10, 0) == []
        # The worker also queued the email about it: once, and without the caller's words.
        async with tenancy.admin.begin() as session:
            await set_tenant_scope(session, tenancy.a.tenant_id)
            alerts = (
                await session.scalars(
                    select(OutboundMessage).where(
                        OutboundMessage.tenant_id == tenancy.a.tenant_id,
                        OutboundMessage.kind == "ticket_alert",
                    )
                )
            ).all()
        assert [(row.channel, row.recipient) for row in alerts] == [
            ("email", "frontdesk@clinic.test")
        ]
        assert "ticket #1" in (alerts[0].subject or "") and "alice" not in alerts[0].body
        today = datetime.now(UTC).date()
        analytics = await query.analytics(tenancy.context(tenancy.a), today, today)
        assert len(analytics) == 1
        assert analytics[0].total == 1 and analytics[0].needs_follow_up == 1
        assert await query.analytics(tenancy.context(tenancy.b), today, today) == []
        # Running the job again opens no second ticket for the same call.
        await activities.record_post_call(request)
        resolved = await tickets.update(
            tenancy.context(tenancy.a), waiting[0].id, UpdateTicket(status="resolved")
        )
        assert resolved.resolved_at is not None and resolved.events[-1].kind == "status"
        assert await tickets.list(tenancy.context(tenancy.a), 10, 0) == []
    finally:
        await worker_db.close()


async def test_worker_runs_the_post_call_job_enqueued_when_a_call_ends(
    tenancy: TenancyFixture,
) -> None:
    assert isinstance(tenancy.settings.worker_database_url, SecretStr)
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
    calls = CallService(tenancy.database.session_factory)
    context = await calls.receive(number, "CA" + uuid4().hex, uuid4())
    state = ConversationStateManager.create_initial_state(
        context.call_id, context.tenant_id, context.provider_call_id
    )
    ConversationStateManager.process_caller_utterance(state, "What are your opening hours?")
    state.append_transcript_turn("agent", "We open at nine.")
    await calls.finish(
        context, "completed", "stream_stopped", post_call_input=PostCallInput.capture(state)
    )
    worker_db = Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    try:
        sessions = worker_db.session_factory
        jobs = JobRunner(WorkflowIntentStore(sessions), WorkflowActivities(sessions))
        # The database is shared with other tests, so drain whatever else is queued.
        for _ in range(500):
            if not await jobs.process_one():
                break
        query = PostCallQueryService(tenancy.database.session_factory)
        report = await query.report(tenancy.context(tenancy.a), context.call_id)
        assert report.outcome.call_status == "completed" and report.turn_count == 2
        async with tenancy.admin.begin() as session:
            intent = await session.scalar(
                select(WorkflowIntent).where(WorkflowIntent.resource_id == context.call_id)
            )
        assert intent is not None and intent.kind == "post_call"
        assert intent.status == "dispatched" and intent.lease_token is None
    finally:
        await worker_db.close()
