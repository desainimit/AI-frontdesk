from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.activities import WorkflowActivities
from app.workflows.definitions import (
    AppointmentReminderWorkflow,
    DocumentIngestionWorkflow,
    PostCallWorkflow,
    TenantOnboardingWorkflow,
)
from app.workflows.dispatch import WorkflowDispatcher
from app.workflows.intent import WorkflowRequest, workflow_id
from app.workflows.store import ClaimedIntent, WorkflowIntentStore


def request() -> WorkflowRequest:
    return WorkflowRequest(str(uuid4()), str(uuid4()), 1)


def test_workflow_ids_are_stable_and_tenant_scoped() -> None:
    first = request()
    assert workflow_id("post_call", first) == workflow_id("post_call", first)
    assert workflow_id("post_call", first) != workflow_id(
        "post_call", WorkflowRequest(str(uuid4()), first.resource_id)
    )
    assert workflow_id("appointment_reminder", first) != workflow_id("post_call", first)


async def test_temporal_workflows_replay_and_wait_outside_voice_path() -> None:
    document_attempts = 0
    reminder_calls = 0

    @activity.defn(name="process_document_job")
    async def process_document_job(_request: WorkflowRequest) -> str:
        nonlocal document_attempts
        document_attempts += 1
        return "pending" if document_attempts == 1 else "delivered"

    @activity.defn(name="record_post_call")
    async def record_post_call(_request: WorkflowRequest) -> str:
        return "recorded"

    @activity.defn(name="record_tenant_onboarding")
    async def record_tenant_onboarding(_request: WorkflowRequest) -> str:
        return "recorded"

    @activity.defn(name="plan_appointment_reminder")
    async def plan_appointment_reminder(_request: WorkflowRequest) -> str:
        return (datetime.now(UTC) + timedelta(minutes=30)).isoformat()

    @activity.defn(name="record_reminder_due")
    async def record_reminder_due(_request: WorkflowRequest) -> str:
        nonlocal reminder_calls
        reminder_calls += 1
        return "due"

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue="phase15-test",
            workflows=[
                DocumentIngestionWorkflow,
                PostCallWorkflow,
                TenantOnboardingWorkflow,
                AppointmentReminderWorkflow,
            ],
            activities=[
                process_document_job,
                record_post_call,
                record_tenant_onboarding,
                plan_appointment_reminder,
                record_reminder_due,
            ],
        ),
    ):
        for kind, workflow_type, expected in (
            ("document_ingestion", DocumentIngestionWorkflow.run, "delivered"),
            ("post_call", PostCallWorkflow.run, "recorded"),
            ("tenant_onboarding", TenantOnboardingWorkflow.run, "recorded"),
            ("appointment_reminder", AppointmentReminderWorkflow.run, "due"),
        ):
            item = request()
            result = await environment.client.execute_workflow(
                workflow_type,
                item,
                id=workflow_id(kind, item),  # type: ignore[arg-type]
                task_queue="phase15-test",
                execution_timeout=timedelta(minutes=45),
            )
            assert result == expected
    assert document_attempts == 2 and reminder_calls == 1


async def test_real_activity_methods_register_with_temporal_worker() -> None:
    activities = WorkflowActivities(MagicMock())
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue="phase15-registration",
            workflows=[PostCallWorkflow],
            activities=[
                activities.process_document_job,
                activities.record_post_call,
                activities.record_tenant_onboarding,
                activities.plan_appointment_reminder,
                activities.record_reminder_due,
            ],
        ),
    ):
        pass


@pytest.mark.parametrize("failure", ["duplicate", "unavailable", "none"])
async def test_dispatcher_recovers_duplicate_starts_and_connection_failures(failure: str) -> None:
    item = request()
    intent = ClaimedIntent(uuid4(), "post_call", item, uuid4(), 1)

    class Store:
        def __init__(self) -> None:
            self.finishes: list[str | None] = []

        async def claim(self) -> ClaimedIntent:
            return intent

        async def finish(self, _intent: ClaimedIntent, *, error: str | None = None) -> bool:
            self.finishes.append(error)
            return True

    class ClientSpy:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def start_workflow(
            self, _workflow: str, _request: WorkflowRequest, **kwargs: Any
        ) -> None:
            self.calls.append(kwargs)
            if failure == "duplicate":
                raise WorkflowAlreadyStartedError(kwargs["id"], "PostCallWorkflow")
            if failure == "unavailable":
                raise OSError("server unavailable")

    store, client = Store(), ClientSpy()
    dispatcher = WorkflowDispatcher(
        cast(WorkflowIntentStore, store), cast(Client, client), "phase15-test"
    )
    assert await dispatcher.process_one()
    assert client.calls[0]["id"] == workflow_id("post_call", item)
    assert store.finishes == (["temporal_unavailable"] if failure == "unavailable" else [None])
