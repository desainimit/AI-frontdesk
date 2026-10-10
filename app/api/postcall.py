"""Authenticated tenant views of post-call reports and follow-up work."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.api.tenancy import Context, Cursor, DatabaseDependency, Limit
from app.core.database.pages import Page, decode_cursor, page
from app.postcall.schemas import DailyCallAnalytics, FollowUpView, PostCallReportView
from app.postcall.service import PostCallQueryService

router = APIRouter(prefix="/api/v1", tags=["post-call"])


@router.get("/calls/{call_id}/post-call")
async def get_report(
    call_id: UUID, context: Context, database: DatabaseDependency
) -> PostCallReportView:
    return await PostCallQueryService(database.session_factory).report(context, call_id)


@router.get("/follow-ups")
async def list_follow_ups(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    # Defaults to the open queue; pass "all" to include acknowledged items.
    status: Literal["pending", "acknowledged", "all"] = "pending",
    kind: Literal["human_review", "appointment_followup"] | None = None,
) -> Page[FollowUpView]:
    offset = decode_cursor(cursor)
    rows = await PostCallQueryService(database.session_factory).follow_ups(
        context, limit + 1, offset, status=None if status == "all" else status, kind=kind
    )
    return page(rows, limit, offset)


@router.post("/follow-ups/{event_id}/acknowledge")
async def acknowledge_follow_up(
    event_id: UUID, context: Context, database: DatabaseDependency
) -> FollowUpView:
    return await PostCallQueryService(database.session_factory).acknowledge(context, event_id)


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
