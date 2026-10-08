"""Transactional intent insertion and deterministic Temporal workflow IDs."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.workflows.models import WorkflowIntent

WorkflowKind = Literal[
    "document_ingestion", "post_call", "tenant_onboarding", "appointment_reminder"
]


@dataclass(frozen=True)
class WorkflowRequest:
    tenant_id: str
    resource_id: str
    generation: int = 1


class WorkflowIntentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session, self._tenant_id = session, tenant_id

    def enqueue(self, kind: WorkflowKind, resource_id: UUID, generation: int = 1) -> None:
        add_intent(self._session, self._tenant_id, kind, resource_id, generation)


def workflow_id(kind: WorkflowKind, request: WorkflowRequest) -> str:
    return f"frontdesk:{kind}:{request.tenant_id}:{request.resource_id}:{request.generation}"


def add_intent(
    session: AsyncSession,
    tenant_id: UUID,
    kind: WorkflowKind,
    resource_id: UUID,
    generation: int = 1,
) -> None:
    session.add(
        WorkflowIntent(
            tenant_id=tenant_id,
            kind=kind,
            resource_id=resource_id,
            generation=generation,
            status="pending",
            attempts=0,
        )
    )
