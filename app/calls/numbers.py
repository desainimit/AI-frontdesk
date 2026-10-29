"""A clinic's phone numbers and whose line each one is.

The platform decides which numbers a clinic has (app.platform.phones). The clinic
decides what each is for: the whole clinic, one location or one provider, and which is
its main number. A provider's number is also its location's. Calls to a line start
with that provider or location in mind; texts about an appointment go out from the
line of its provider, else its location, else the main number.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.models import PhoneNumber
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.locations.models import Location
from app.providers.models import Provider


class LineTakenError(ConflictError):
    code = "line_taken"
    public_message = "That provider already has a number. Free it there first."


class NumberView(BaseModel):
    id: UUID
    number: str
    label: str
    location_id: UUID | None
    location_name: str | None = None
    provider_id: UUID | None
    provider_name: str | None = None
    is_main: bool


class AssignLine(BaseModel):
    """What one number is for. Neither a location nor a provider: the whole clinic."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(default="", max_length=60, pattern=r"^[^\n<>{}]*$")
    location_id: UUID | None = None
    provider_id: UUID | None = None
    is_main: bool = False


class Line(BaseModel):
    """Whose line a call came in on, for the receptionist."""

    provider_id: UUID | None = None
    provider_name: str | None = None
    location_id: UUID | None = None
    location_name: str | None = None


def _query():
    return (
        select(PhoneNumber, Provider.display_name, Location.name)
        .outerjoin(
            Provider,
            (Provider.tenant_id == PhoneNumber.tenant_id)
            & (Provider.id == PhoneNumber.provider_id),
        )
        .outerjoin(
            Location,
            (Location.tenant_id == PhoneNumber.tenant_id)
            & (Location.id == PhoneNumber.location_id),
        )
        .where(PhoneNumber.is_active.is_(True))
    )


def _view(row: PhoneNumber, provider: str | None, location: str | None) -> NumberView:
    return NumberView(
        id=row.id,
        number=row.number,
        label=row.label,
        location_id=row.location_id,
        location_name=location,
        provider_id=row.provider_id,
        provider_name=provider,
        is_main=row.is_main,
    )


class ClinicNumbers:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list(self, context: RequestContext) -> list[NumberView]:
        """The numbers the platform gave this clinic, and whose line each is."""
        require_permission(context, Permission.SETTINGS_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            rows = await session.execute(
                _query()
                .where(PhoneNumber.tenant_id == context.tenant_id)
                .order_by(PhoneNumber.is_main.desc(), PhoneNumber.created_at)
            )
            return [_view(row, provider, location) for row, provider, location in rows]

    async def assign(
        self, context: RequestContext, number_id: UUID, command: AssignLine
    ) -> NumberView:
        """Say what a number is for. Only these fields change; which numbers the clinic
        has stays with the platform."""
        require_permission(context, Permission.SETTINGS_UPDATE)
        try:
            async with self._sessions.begin() as session:
                await enter_tenant_scope(session, context)
                phone = await session.scalar(
                    select(PhoneNumber)
                    .where(
                        PhoneNumber.tenant_id == context.tenant_id,
                        PhoneNumber.id == number_id,
                        PhoneNumber.is_active.is_(True),
                    )
                    .with_for_update()
                )
                if phone is None:
                    raise NotFoundError()
                location_id = command.location_id
                if command.provider_id is not None:
                    provider = await session.scalar(
                        select(Provider).where(
                            Provider.tenant_id == context.tenant_id,
                            Provider.id == command.provider_id,
                        )
                    )
                    if provider is None or not provider.is_active:
                        raise ValidationFailedError(
                            [("provider_id", "Choose an active provider of this clinic.")]
                        )
                    if location_id is not None and location_id != provider.location_id:
                        raise ValidationFailedError(
                            [("location_id", "A provider's number is for the provider's location.")]
                        )
                    location_id = provider.location_id
                    taken = await session.scalar(
                        select(PhoneNumber.id).where(
                            PhoneNumber.tenant_id == context.tenant_id,
                            PhoneNumber.provider_id == provider.id,
                            PhoneNumber.is_active.is_(True),
                            PhoneNumber.id != phone.id,
                        )
                    )
                    if taken is not None:
                        raise LineTakenError()
                elif location_id is not None:
                    found = await session.scalar(
                        select(Location.id).where(
                            Location.tenant_id == context.tenant_id, Location.id == location_id
                        )
                    )
                    if found is None:
                        raise ValidationFailedError(
                            [("location_id", "Choose a location of this clinic.")]
                        )
                if phone.is_main and not command.is_main:
                    others = await session.scalar(
                        select(PhoneNumber.id).where(
                            PhoneNumber.tenant_id == context.tenant_id,
                            PhoneNumber.is_active.is_(True),
                            PhoneNumber.id != phone.id,
                        )
                    )
                    if others is not None:
                        raise ValidationFailedError(
                            [("is_main", "Make another number the main one instead.")]
                        )
                if command.is_main and not phone.is_main:
                    await session.execute(
                        update(PhoneNumber)
                        .where(
                            PhoneNumber.tenant_id == context.tenant_id,
                            PhoneNumber.is_main.is_(True),
                        )
                        .values(is_main=False)
                    )
                phone.label = command.label
                phone.location_id = location_id
                phone.provider_id = command.provider_id
                # A clinic's only number is always its main one.
                phone.is_main = command.is_main or phone.is_main
                await session.flush()
                session.add(
                    AuditEvent(
                        tenant_id=context.tenant_id,
                        actor_id=context.actor_id,
                        platform_user_id=context.platform_user_id,
                        support_session_id=context.support_session_id,
                        actor_type=context.actor_type,
                        action="phone_line_assigned",
                        resource_type="phone_number",
                        resource_id=phone.id,
                        request_id=context.request_id,
                    )
                )
                row = (await session.execute(_query().where(PhoneNumber.id == phone.id))).one()
                return _view(*row)
        except IntegrityError:
            # Two administrators gave the same provider a number at once.
            raise LineTakenError() from None


async def line_for(session: AsyncSession, tenant_id: UUID, phone_number_id: UUID) -> Line:
    """Whose line a number is, for a call that came in on it."""
    row = (
        await session.execute(
            _query().where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.id == phone_number_id)
        )
    ).first()
    if row is None:
        return Line()
    phone, provider, location = row
    return Line(
        provider_id=phone.provider_id,
        provider_name=provider,
        location_id=phone.location_id,
        location_name=location,
    )


async def sender_for(
    session: AsyncSession, tenant_id: UUID, provider_id: UUID | None, location_id: UUID | None
) -> str | None:
    """The number a text goes out from: the provider's line, else the location's, else
    the clinic's main number, else its oldest."""
    rows = (
        await session.execute(
            select(PhoneNumber.number, PhoneNumber.provider_id, PhoneNumber.location_id)
            .where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.is_active.is_(True))
            .order_by(PhoneNumber.is_main.desc(), PhoneNumber.created_at)
        )
    ).all()
    lines: list[tuple[str, UUID | None, UUID | None]] = [
        (number, provider, location) for number, provider, location in rows
    ]
    if provider_id is not None:
        found = next((number for number, provider, _ in lines if provider == provider_id), None)
        if found is not None:
            return found
    if location_id is not None:
        found = next(
            (
                number
                for number, provider, location in lines
                if provider is None and location == location_id
            ),
            None,
        )
        if found is not None:
            return found
    return lines[0][0] if lines else None
