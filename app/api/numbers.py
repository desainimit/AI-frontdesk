"""A clinic's own view of its phone numbers: whose line each one is."""

from uuid import UUID

from fastapi import APIRouter

from app.api.tenancy import Context, DatabaseDependency
from app.calls.numbers import AssignLine, ClinicNumbers, NumberView

router = APIRouter(prefix="/api/v1/tenants/current", tags=["phone numbers"])


@router.get("/phone-numbers")
async def clinic_numbers(context: Context, database: DatabaseDependency) -> list[NumberView]:
    """The numbers the platform gave this clinic, main number first."""
    return await ClinicNumbers(database.session_factory).list(context)


@router.patch("/phone-numbers/{number_id}")
async def assign_line(
    number_id: UUID, command: AssignLine, context: Context, database: DatabaseDependency
) -> NumberView:
    """Make a number the whole clinic's, a location's or a provider's, or the main one."""
    return await ClinicNumbers(database.session_factory).assign(context, number_id, command)
