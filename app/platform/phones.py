"""Phone numbers on the platform's phone account, and which clinic each one serves.

Assigning a number proves the account owns it, routes it to the clinic and points the
number's call webhooks at this application, so nobody pastes URLs into the provider.
"""

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


class PlatformNumber(BaseModel):
    number: str
    # The clinic this number serves; empty while it is free.
    tenant_id: UUID | None = None
    tenant_name: str | None = None
    is_active: bool = False
    # False when the provider still has to be pointed at this application by hand.
    webhooks_configured: bool | None = None


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
        """Route a proven number to a clinic. Needs the clinic's scope on the session."""
        try:
            phone = await session.scalar(
                select(PhoneNumber).where(PhoneNumber.number == match.number).with_for_update()
            )
            if phone is not None and phone.tenant_id != tenant_id:
                raise NumberTakenError()
            if phone is None:
                phone = PhoneNumber(tenant_id=tenant_id, number=match.number)
                session.add(phone)
            phone.is_active, phone.provider_number_sid = True, match.sid
            # A number on the platform account is not tied to a clinic's own connection.
            phone.integration_id = None
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
                select(PhoneNumber).where(PhoneNumber.number == number).with_for_update()
            )
            if phone is None:
                raise NotFoundError()
            phone.is_active = False
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    action="phone_number_released",
                    request_id=context.request_id,
                    resource_id=phone.id,
                    tenant_id=phone.tenant_id,
                )
            )

    async def point_webhooks(self, sid: str) -> bool:
        """Tell the provider to send this number's calls here. False when it could not."""
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
