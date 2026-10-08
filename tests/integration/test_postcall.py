from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.calls.models import PhoneNumber
from app.calls.service import CallService
from app.conversation.manager import ConversationStateManager
from app.database.session import Database
from app.errors import NotFoundError
from app.postcall.schemas import PostCallInput
from app.postcall.service import PostCallQueryService
from app.workflows.activities import WorkflowActivities
from app.workflows.intent import WorkflowRequest
from app.workflows.models import WorkflowIntent
from app.workflows.runner import JobRunner
from app.workflows.store import WorkflowIntentStore
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_post_call_callback_race_reports_follow_up_and_tenant_analytics(
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
        assert await activities.record_post_call(request) == "recorded"
        assert await activities.record_post_call(request) == "recorded"
        query = PostCallQueryService(tenancy.database.session_factory)
        report = await query.report(tenancy.context(tenancy.a), context.call_id)
        assert report.outcome.call_status == "failed"
        assert report.outcome.follow_up == "human_review"
        assert "alice@example.com" not in report.summary
        with pytest.raises(NotFoundError):
            await query.report(tenancy.context(tenancy.b), context.call_id)
        follow_ups = await query.follow_ups(tenancy.context(tenancy.a), 10, 0)
        assert len(follow_ups) == 1 and follow_ups[0].kind == "human_review"
        today = datetime.now(UTC).date()
        analytics = await query.analytics(tenancy.context(tenancy.a), today, today)
        assert len(analytics) == 1
        assert analytics[0].total == 1 and analytics[0].needs_follow_up == 1
        assert await query.analytics(tenancy.context(tenancy.b), today, today) == []
        await query.acknowledge(tenancy.context(tenancy.a), follow_ups[0].id)
        assert await query.follow_ups(tenancy.context(tenancy.a), 10, 0) == []
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
