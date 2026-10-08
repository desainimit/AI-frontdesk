"""Transactional job intents: work recorded with the change that requires it."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.workflows.models import WorkflowIntent

WorkflowKind = Literal["post_call"]


@dataclass(frozen=True)
class WorkflowRequest:
    tenant_id: str
    resource_id: str
    generation: int = 1


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
