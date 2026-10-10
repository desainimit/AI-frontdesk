"""Connecting a provider's Google or Outlook calendar."""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.tenancy import Context, DatabaseDependency
from app.calendar_sync.service import (
    CalendarOptions,
    CalendarService,
    CalendarStart,
    CalendarStatus,
    CompleteCalendar,
    ConnectCalendar,
)

router = APIRouter(prefix="/api/v1", tags=["calendar"])


def _service(request: Request, database: DatabaseDependency) -> CalendarService:
    return CalendarService(
        database.session_factory,
        request.app.state.settings,
        getattr(request.app.state, "provider_transport", None),
    )


@router.get("/calendar")
async def calendar_options(
    request: Request, context: Context, database: DatabaseDependency
) -> CalendarOptions:
    """Each provider's calendar status, and which kinds of calendar can be connected."""
    return await _service(request, database).options(context)


@router.post("/providers/{provider_id}/calendar/connect")
async def connect_calendar(
    provider_id: UUID,
    command: ConnectCalendar,
    request: Request,
    context: Context,
    database: DatabaseDependency,
) -> CalendarStart:
    """Start connecting: answers with the address to send the calendar's owner to."""
    return await _service(request, database).start(context, provider_id, command)


@router.post("/calendar/complete")
async def complete_calendar(
    command: CompleteCalendar, request: Request, context: Context, database: DatabaseDependency
) -> CalendarStatus:
    """Finish the sign-in the browser came back from."""
    return await _service(request, database).complete(context, command)


@router.delete("/providers/{provider_id}/calendar", status_code=204)
async def disconnect_calendar(
    provider_id: UUID, request: Request, context: Context, database: DatabaseDependency
) -> Response:
    """Stop syncing. Events already in the calendar are left as they are."""
    await _service(request, database).disconnect(context, provider_id)
    return Response(status_code=204)
