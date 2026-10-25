"""Authenticated tenant views of post-call reports and call analytics."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.api.tenancy import Context, DatabaseDependency
from app.postcall.schemas import DailyCallAnalytics, PostCallReportView, TranscriptLine
from app.postcall.service import PostCallQueryService

router = APIRouter(prefix="/api/v1", tags=["post-call"])


@router.get("/calls/{call_id}/post-call")
async def get_report(
    call_id: UUID, context: Context, database: DatabaseDependency
) -> PostCallReportView:
    return await PostCallQueryService(database.session_factory).report(context, call_id)


@router.get("/calls/{call_id}/transcript")
async def get_transcript(
    call_id: UUID, context: Context, database: DatabaseDependency
) -> list[TranscriptLine]:
    """The conversation of a finished call, as a thread."""
    return await PostCallQueryService(database.session_factory).transcript(context, call_id)


@router.get("/analytics/calls")
async def call_analytics(
    context: Context,
    database: DatabaseDependency,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
) -> list[DailyCallAnalytics]:
    if end < start or (end - start).days > 366:
        raise HTTPException(422, "Invalid analytics date range")
    return await PostCallQueryService(database.session_factory).analytics(context, start, end)
