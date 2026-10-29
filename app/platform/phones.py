"""Phone numbers on the platform's phone account, and which clinic each one serves.

Assigning a number proves the account owns it, routes it to the clinic and points the
number's call webhooks at this application, so nobody pastes URLs into the provider.
"""

from typing import Any, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.calls.models import PhoneNumber
from app.core.database.rls import set_tenant_scope
from app.core.errors import ApplicationError, ConflictError, NotFoundError
from app.core.settings import Settings
from app.identity.sessions import platform_scope
from app.integrations.verifiers import (
    HttpProviderVerifier,
    ProviderNumber,
    ProviderUnavailableError,
)
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.tenants.models import Tenant

TWILIO_TIMEOUT_SECONDS = 8


class PhoneServiceMissingError(ApplicationError):
    status_code = 409
    code = "phone_service_missing"
    public_message = "Connect and verify the phone service in platform settings first."


class PhoneServiceUnavailableError(ApplicationError):
    status_code = 503
    code = "phone_service_unavailable"
    public_message = "The phone service could not be reached. Try again in a moment."


class NumberNotOwnedError(ApplicationError):
    status_code = 422
    code = "number_not_owned"
    public_message = "The connected phone account does not own this number."


class NumberTakenError(ConflictError):
    code = "number_taken"
    public_message = "This number already serves another clinic. Release it there first."


class AssignNumber(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")


class NumberUnavailableError(ConflictError):
    code = "number_unavailable"
    public_message = "That number was just taken by someone else. Search again and pick another."


class NumberNeedsRegistrationError(ApplicationError):
    status_code = 422
    code = "number_needs_registration"
    public_message = (
        "The phone service needs an address or a regulatory registration for numbers in this "
        "country before it sells one. Complete it in the Twilio console, then try again."
    )


class NumberRefusedError(ApplicationError):
    status_code = 422
    code = "number_refused"
    public_message = "The phone service would not sell this number. Try another one."


# Twilio's error codes for a number that cannot be bought as asked.
_NEEDS_REGISTRATION = {21631, 21649, 21612, 21615, 21628}
_UNAVAILABLE = {21422, 21452}


class FindNumbers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str = Field(default="IN", pattern=r"^[A-Z]{2}$")
    # Digits the number should contain, such as an area code.
    contains: str = Field(default="", pattern=r"^[0-9]{0,10}$")


class AvailableNumber(BaseModel):
    number: str
    locality: str = ""
    region: str = ""
    # What the number costs each month, as the phone service prices it; empty when it
    # does not say.
    monthly_price: str = ""
    currency: str = ""


class BuyNumber(AssignNumber):
    # The monthly price the administrator was shown and agreed to.
    monthly_price: str = Field(default="", max_length=20)


class PlatformNumber(BaseModel):
    number: str
    # The clinic this number serves; empty while it is free.
    tenant_id: UUID | None = None
    tenant_name: str | None = None
    is_active: bool = False
    # False when the provider still has to be pointed at this application by hand.
    webhooks_configured: bool | None = None


async def _has_main(session: AsyncSession, tenant_id: UUID, other_than: UUID) -> bool:
    return (
        await session.scalar(
            select(PhoneNumber.id).where(
                PhoneNumber.tenant_id == tenant_id,
                PhoneNumber.is_active.is_(True),
                PhoneNumber.is_main.is_(True),
                PhoneNumber.id != other_than,
            )
        )
        is not None
    )


async def promote_main(session: AsyncSession, tenant_id: UUID) -> None:
    """Make the clinic's oldest active number its main one, when it has none."""
    numbers = (
        await session.scalars(
            select(PhoneNumber)
            .where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.is_active.is_(True))
            .order_by(PhoneNumber.created_at)
            .with_for_update()
        )
    ).all()
    if numbers and not any(number.is_main for number in numbers):
        numbers[0].is_main = True
        await session.flush()


class PlatformPhoneService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions, self._settings, self._transport = sessions, settings, transport

    def _account(self) -> tuple[str, dict[str, str]]:
        if not self._settings.platform_twilio:
            raise PhoneServiceMissingError()
        return (
            self._settings.twilio_account_sid,
            {"auth_token": self._settings.twilio_auth_token.get_secret_value()},
        )

    async def numbers(self, context: PlatformContext) -> list[PlatformNumber]:
        """Every number on the platform's phone account, with the clinic it serves."""
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        account, secrets = self._account()
        try:
            owned = await HttpProviderVerifier(
                TWILIO_TIMEOUT_SECONDS, self._transport
            ).twilio_numbers(account, secrets)
        except ProviderUnavailableError:
            raise PhoneServiceUnavailableError() from None
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            assigned = {
                row.number: (row, name)
                for row, name in await session.execute(
                    select(PhoneNumber, Tenant.name)
                    .join(Tenant, Tenant.id == PhoneNumber.tenant_id)
                    .where(PhoneNumber.number.in_([item.number for item in owned]))
                    # A released number's old rows first, so the active row is kept.
                    .order_by(PhoneNumber.is_active, PhoneNumber.created_at)
                )
            }
        result: list[PlatformNumber] = []
        for item in sorted(owned, key=lambda entry: entry.number):
            row, name = assigned.get(item.number, (None, None))
            result.append(
                PlatformNumber(
                    number=item.number,
                    tenant_id=row.tenant_id if row else None,
                    tenant_name=name,
                    is_active=bool(row and row.is_active),
                )
            )
        return result

    def _auth(self) -> httpx.BasicAuth:
        account, secrets = self._account()
        return httpx.BasicAuth(account, secrets["auth_token"])

    async def available(
        self, context: PlatformContext, query: FindNumbers
    ) -> list[AvailableNumber]:
        """Numbers the phone service can sell now, that take calls and texts, with their
        monthly price. Looking costs nothing."""
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        account, _ = self._account()
        params = {"VoiceEnabled": "true", "SmsEnabled": "true", "PageSize": "20"}
        if query.contains:
            params["Contains"] = query.contains
        base = f"https://api.twilio.com/2010-04-01/Accounts/{account}"
        try:
            async with httpx.AsyncClient(
                timeout=TWILIO_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                found = await client.get(
                    f"{base}/AvailablePhoneNumbers/{query.country}/Local.json",
                    params=params,
                    auth=self._auth(),
                )
                priced = await client.get(
                    f"https://pricing.twilio.com/v1/PhoneNumbers/Countries/{query.country}",
                    auth=self._auth(),
                )
        except httpx.HTTPError:
            raise PhoneServiceUnavailableError() from None
        if found.status_code == 404:
            return []
        if found.status_code != 200:
            raise PhoneServiceUnavailableError()
        price, currency = "", ""
        if priced.status_code == 200:
            body = cast(dict[str, Any], priced.json())
            currency = str(body.get("price_unit") or "")
            prices = cast(list[dict[str, Any]], body.get("phone_number_prices") or [])
            local = next((item for item in prices if item.get("number_type") == "local"), None)
            price = str((local or {}).get("current_price") or "")
        listed = cast(
            list[dict[str, Any]],
            cast(dict[str, Any], found.json()).get("available_phone_numbers") or [],
        )
        return [
            AvailableNumber(
                number=str(item.get("phone_number")),
                locality=str(item.get("locality") or ""),
                region=str(item.get("region") or ""),
                monthly_price=price,
                currency=currency,
            )
            for item in listed
            if item.get("phone_number")
        ]

    async def buy(self, context: PlatformContext, command: BuyNumber) -> PlatformNumber:
        """Buy a number on the platform's phone account and give it to a clinic.

        Asking twice buys once: a number the account already owns is only assigned.
        """
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        try:
            await self.prove(command.number)
        except NumberNotOwnedError:
            await self._purchase(command.number)
            async with self._sessions.begin() as session:
                await platform_scope(session, context)
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        action="phone_number_bought",
                        request_id=context.request_id,
                        tenant_id=command.tenant_id,
                    )
                )
        return await self.assign(
            context, AssignNumber(tenant_id=command.tenant_id, number=command.number)
        )

    async def _purchase(self, number: str) -> None:
        account, _ = self._account()
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account}/IncomingPhoneNumbers.json"
        try:
            async with httpx.AsyncClient(
                timeout=TWILIO_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.post(url, data={"PhoneNumber": number}, auth=self._auth())
        except httpx.HTTPError:
            raise PhoneServiceUnavailableError() from None
        if response.status_code in (200, 201):
            return
        try:
            code = int(response.json().get("code") or 0)
        except (ValueError, AttributeError):
            code = 0
        if code in _NEEDS_REGISTRATION:
            raise NumberNeedsRegistrationError()
        if code in _UNAVAILABLE:
            raise NumberUnavailableError()
        if response.status_code >= 500 or response.status_code == 429:
            raise PhoneServiceUnavailableError()
        raise NumberRefusedError()

    async def prove(self, number: str) -> ProviderNumber:
        """Ask the phone service whether the platform account owns this number."""
        account, secrets = self._account()
        try:
            # Ownership is proven by the provider, never by a typed number.
            owned = await HttpProviderVerifier(
                TWILIO_TIMEOUT_SECONDS, self._transport
            ).twilio_numbers(account, secrets, number)
        except ProviderUnavailableError:
            raise PhoneServiceUnavailableError() from None
        match = next((item for item in owned if item.number == number), None)
        if match is None:
            raise NumberNotOwnedError()
        return match

    @staticmethod
    async def attach(session: AsyncSession, tenant_id: UUID, match: ProviderNumber) -> PhoneNumber:
        """Route a proven number to a clinic. Needs the clinic's scope on the session.

        A number released by another clinic can be given to this one: its old row stays
        with the old clinic's calls, and this clinic gets a row of its own.
        """
        try:
            rows = (
                await session.scalars(
                    select(PhoneNumber)
                    .where(PhoneNumber.number == match.number)
                    .order_by(PhoneNumber.created_at.desc())
                    .with_for_update()
                )
            ).all()
            if any(row.is_active and row.tenant_id != tenant_id for row in rows):
                raise NumberTakenError()
            phone = next((row for row in rows if row.tenant_id == tenant_id), None)
            if phone is None:
                phone = PhoneNumber(tenant_id=tenant_id, number=match.number)
                session.add(phone)
            phone.is_active, phone.provider_number_sid = True, match.sid
            # A number on the platform account is not tied to a clinic's own connection.
            phone.integration_id = None
            await session.flush()
            # A clinic's first number is its main one.
            if not await _has_main(session, tenant_id, phone.id):
                phone.is_main = True
                await session.flush()
        except IntegrityError:
            raise NumberTakenError() from None
        return phone

    async def assign(self, context: PlatformContext, command: AssignNumber) -> PlatformNumber:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        match = await self.prove(command.number)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == command.tenant_id, Tenant.is_active.is_(True))
            )
            if tenant is None:
                raise NotFoundError()
            await set_tenant_scope(session, tenant.id)
            phone = await self.attach(session, tenant.id, match)
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    action="phone_number_assigned",
                    request_id=context.request_id,
                    resource_id=phone.id,
                    tenant_id=tenant.id,
                )
            )
            name = tenant.name
        return PlatformNumber(
            number=command.number,
            tenant_id=command.tenant_id,
            tenant_name=name,
            is_active=True,
            webhooks_configured=await self.point_webhooks(match.sid),
        )

    async def release(self, context: PlatformContext, number: str) -> None:
        """Stop routing a number to its clinic. Its call history stays."""
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            phone = await session.scalar(
                select(PhoneNumber)
                .where(PhoneNumber.number == number, PhoneNumber.is_active.is_(True))
                .with_for_update()
            )
            if phone is None:
                raise NotFoundError()
            was_main = phone.is_main
            # Free for another clinic; whose line it was goes with the release.
            phone.is_active, phone.is_main = False, False
            phone.location_id = phone.provider_id = None
            await session.flush()
            if was_main:
                await promote_main(session, phone.tenant_id)
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    action="phone_number_released",
                    request_id=context.request_id,
                    resource_id=phone.id,
                    tenant_id=phone.tenant_id,
                )
            )

    async def repoint(self, context: PlatformContext, number: str) -> PlatformNumber:
        """Send an assigned number's calls and texts here again.

        For when the application's public address changed after the number was assigned.
        """
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        match = await self.prove(number)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = (
                await session.execute(
                    select(PhoneNumber, Tenant.name)
                    .join(Tenant, Tenant.id == PhoneNumber.tenant_id)
                    .where(PhoneNumber.number == number, PhoneNumber.is_active.is_(True))
                )
            ).first()
            if row is None:
                raise NotFoundError()
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    action="phone_number_repointed",
                    request_id=context.request_id,
                    resource_id=row[0].id,
                    tenant_id=row[0].tenant_id,
                )
            )
        return PlatformNumber(
            number=number,
            tenant_id=row[0].tenant_id,
            tenant_name=row[1],
            is_active=True,
            webhooks_configured=await self.point_webhooks(match.sid),
        )

    async def point_webhooks(self, sid: str) -> bool:
        """Tell the provider to send this number's calls and texts here. False when it
        could not."""
        account, secrets = self._account()
        base = self._settings.telephony_public_base_url
        if not base:
            return False
        try:
            async with httpx.AsyncClient(
                timeout=TWILIO_TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport
            ) as client:
                response = await client.post(
                    "https://api.twilio.com/2010-04-01/Accounts/"
                    f"{account}/IncomingPhoneNumbers/{sid}.json",
                    data={
                        "VoiceUrl": f"{base}/webhooks/v1/twilio/incoming",
                        "VoiceMethod": "POST",
                        "StatusCallback": f"{base}/webhooks/v1/twilio/status",
                        "StatusCallbackMethod": "POST",
                        "SmsUrl": f"{base}/webhooks/v1/twilio/sms",
                        "SmsMethod": "POST",
                    },
                    auth=httpx.BasicAuth(account, secrets["auth_token"]),
                )
        except httpx.HTTPError:
            return False
        return response.status_code == 200
