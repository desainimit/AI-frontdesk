"""Replay-safe Temporal workflows; all database and provider work is in activities."""

from datetime import datetime, timedelta
from typing import cast

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.intent import WorkflowRequest


SHORT_RETRY = RetryPolicy(maximum_attempts=3)


@workflow.defn
class DocumentIngestionWorkflow:
    @workflow.run
    async def run(self, request: WorkflowRequest) -> str:
        while True:
            status = cast(
                str,
                await workflow.execute_activity(
                    "process_document_job",
                    request,
                    start_to_close_timeout=timedelta(minutes=16),
                    retry_policy=SHORT_RETRY,
                ),
            )
            if status in {"delivered", "dead", "ready"}:
                return status
            await workflow.sleep(30)


@workflow.defn
class PostCallWorkflow:
    @workflow.run
    async def run(self, request: WorkflowRequest) -> str:
        while True:
            status = cast(
                str,
                await workflow.execute_activity(
                    "record_post_call",
                    request,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=SHORT_RETRY,
                ),
            )
            if status != "pending":
                return status
            await workflow.sleep(5)


@workflow.defn
class TenantOnboardingWorkflow:
    @workflow.run
    async def run(self, request: WorkflowRequest) -> str:
        return cast(
            str,
            await workflow.execute_activity(
                "record_tenant_onboarding",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=SHORT_RETRY,
            ),
        )


@workflow.defn
class AppointmentReminderWorkflow:
    @workflow.run
    async def run(self, request: WorkflowRequest) -> str:
        due = cast(
            str | None,
            await workflow.execute_activity(
                "plan_appointment_reminder",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=SHORT_RETRY,
            ),
        )
        if due is None:
            return "skipped"
        seconds = (datetime.fromisoformat(due) - workflow.now()).total_seconds()
        if seconds > 0:
            await workflow.sleep(seconds)
        return cast(
            str,
            await workflow.execute_activity(
                "record_reminder_due",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=SHORT_RETRY,
            ),
        )
