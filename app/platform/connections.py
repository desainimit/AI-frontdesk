"""The platform's own provider accounts and product settings.

A platform administrator saves these in the product. They are stored in the database
(secrets as ciphertext) and copied onto the running process's settings, so every part
of the application that used to read an environment variable now reads what the
administrator saved. A clinic without its own connection is served by these.
"""

import asyncio
import json
import logging
import smtplib
import ssl
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError, NotFoundError, ValidationFailedError
from app.core.security.encryption import SecretCipher
from app.core.settings import PLATFORM_MANAGED, Settings, SpeakingSpeed
from app.identity.mail import SMTP_TIMEOUT_SECONDS, send_smtp
from app.identity.sessions import platform_scope
from app.integrations.catalog import (
    PLATFORM_CATALOG,
    InvalidIntegrationError,
    Purpose,
    platform_spec,
    validate_config,
    validate_secrets,
)
from app.integrations.verifiers import HttpProviderVerifier, ProviderVerifier, Verification
from app.platform.models import PlatformAuditEvent, PlatformConnection, PlatformSettings
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission

logger = logging.getLogger(__name__)

PLATFORM_PURPOSES = (
    Purpose.EMAIL,
    Purpose.TELEPHONY,
    Purpose.STT,
    Purpose.TTS,
    Purpose.LLM,
    Purpose.GOOGLE_CALENDAR,
    Purpose.MICROSOFT_CALENDAR,
)
# A calendar app is proven the first time a provider connects through it; there is
# nothing to ask Google or Microsoft with the app's details alone.
CALENDAR_APPS = {Purpose.GOOGLE_CALENDAR: "google", Purpose.MICROSOFT_CALENDAR: "microsoft"}
# How long another process may run on a provider account that was just changed.
REFRESH_SECONDS = 15
# What a failed check means, in words an administrator can act on.
PROBLEMS = {
    "credentials_rejected": "The provider did not accept these credentials.",
    "missing_permissions": (
        "This key is restricted. In ElevenLabs, edit the key and turn on Text to Speech."
    ),
    "account_not_found": "The provider has no account with this ID.",
    "account_not_active": "This provider account is not active.",
    "api_key_rejected": "The provider did not accept the API key.",
    "voice_not_found": (
        "This account has no voice with this ID. Use the ID of a voice listed under "
        "My Voices in ElevenLabs."
    ),
    "model_unavailable": "This account cannot use the selected model.",
    "model_missing_features": (
        "One of the chosen models cannot use the receptionist's tools or answer in the "
        "required format right now. Choose another model for that job."
    ),
    "model_not_private": (
        "One of the chosen models has no endpoint that keeps nothing of the "
        "conversation. Choose another model for that job."
    ),
    "provider_unavailable": "The provider could not be reached. Try again in a moment.",
    "server_unreachable": "The email server could not be reached. Check the host and port.",
    "secure_connection_failed": "The email server refused a secure connection.",
    "encryption_required": (
        "The email server did not accept the sign-in without encryption. "
        "Choose STARTTLS and try again."
    ),
}


class InvalidConnectionError(ApplicationError):
    status_code = 422
    code = "invalid_connection"
    public_message = "These connection details are not valid. Check each field and try again."


class MailNotReadyError(ApplicationError):
    status_code = 409
    code = "mail_not_ready"
    public_message = "Save and verify the email server before sending a test message."


class TestMailFailedError(ApplicationError):
    status_code = 502
    code = "test_mail_failed"
    public_message = "The email server did not accept the test message."


class SaveConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(pattern=r"^[a-z_]{2,30}$")
    account_identifier: str | None = Field(default=None, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    # Leave out to keep the saved secret and change only the other details.
    secrets: dict[str, Any] | None = None


class ConnectionView(BaseModel):
    purpose: Purpose
    configured: bool
    provider: str | None = None
    provider_label: str | None = None
    account_identifier: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    secret_hint: str | None = None
    status: Literal["not_configured", "saved_unverified", "verified", "invalid"] = "not_configured"
    problem: str | None = None
    last_checked_at: datetime | None = None
    updated_at: datetime | None = None


class TestMail(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    to: str = Field(max_length=254, pattern=r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class ProductSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, from_attributes=True)
    product_name: str = Field(min_length=1, max_length=100)
    support_email: str = Field(default="", max_length=254, pattern=r"^(|[^\s@]+@[^\s@]+\.[^\s@]+)$")
    session_minutes: int = Field(ge=5, le=720)
    speaking_speed: SpeakingSpeed = "normal"


def _context(purpose: str, provider: str) -> str:
    return f"platform-connection:{purpose}:{provider}"


def _view(purpose: Purpose, row: PlatformConnection | None) -> ConnectionView:
    if row is None:
        return ConnectionView(purpose=purpose, configured=False)
    return ConnectionView(
        purpose=purpose,
        configured=True,
        provider=row.provider,
        provider_label=PLATFORM_CATALOG[(purpose, row.provider)].label,
        account_identifier=row.account_identifier,
        config=dict(row.config),
        secret_hint=f"ends in {row.display_suffix}" if row.display_suffix else None,
        status=row.status,  # pyright: ignore[reportArgumentType]
        problem=PROBLEMS.get(row.error_code or "", "The check failed.") if row.error_code else None,
        last_checked_at=row.last_checked_at,
        updated_at=row.updated_at,
    )


def _check_smtp(config: dict[str, Any], password: str) -> Verification:
    """Connect, secure the connection and sign in. Sends nothing."""
    offers_encryption = False
    try:
        if config["tls_mode"] == "tls":
            connection: smtplib.SMTP = smtplib.SMTP_SSL(
                config["host"],
                config["port"],
                timeout=SMTP_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        else:
            connection = smtplib.SMTP(config["host"], config["port"], timeout=SMTP_TIMEOUT_SECONDS)
        with connection:
            if config["tls_mode"] == "starttls":
                connection.starttls(context=ssl.create_default_context())
            elif config["tls_mode"] == "plain":
                connection.ehlo_or_helo_if_needed()
                offers_encryption = connection.has_extn("starttls")
            if config["username"]:
                connection.login(config["username"], password)
            connection.noop()
    except smtplib.SMTPAuthenticationError:
        # Most servers accept a sign-in only once the connection is encrypted, and say
        # nothing more than "rejected" when it is not.
        if offers_encryption:
            return Verification("invalid", "encryption_required")
        return Verification("invalid", "credentials_rejected")
    except (smtplib.SMTPNotSupportedError, ssl.SSLError):
        return Verification("invalid", "secure_connection_failed")
    except (smtplib.SMTPException, OSError):
        return Verification("invalid", "server_unreachable")
    return Verification("verified", "ok")


def platform_values(
    connections: list[tuple[PlatformConnection, dict[str, str]]], product: PlatformSettings | None
) -> dict[str, Any]:
    """Settings fields for the verified connections; everything else back to its default."""
    values: dict[str, Any] = {
        name: Settings.model_fields[name].get_default(call_default_factory=True)
        for name in PLATFORM_MANAGED
    }
    for row, secrets in connections:
        config = row.config
        if row.provider in {"language_router", "sarvam_router"}:
            prefix = "stt" if row.purpose == Purpose.STT else "tts"
            values[f"{prefix}_provider"] = row.provider
            values[f"routed_{prefix}_config"] = dict(config)
            values[f"routed_{prefix}_secrets"] = {
                name: SecretStr(value) for name, value in secrets.items()
            }
        elif row.purpose in {Purpose.GOOGLE_CALENDAR, Purpose.MICROSOFT_CALENDAR}:
            prefix = CALENDAR_APPS[Purpose(row.purpose)]
            values[f"{prefix}_calendar_client_id"] = row.account_identifier or ""
            values[f"{prefix}_calendar_client_secret"] = SecretStr(secrets["client_secret"])
        elif row.provider == "twilio":
            values["twilio_account_sid"] = row.account_identifier or ""
            values["twilio_auth_token"] = SecretStr(secrets["auth_token"])
        elif row.provider == "deepgram":
            values.update(
                stt_provider="deepgram",
                deepgram_api_key=SecretStr(secrets["api_key"]),
                deepgram_model=config["model"],
                deepgram_language=config["language"],
            )
        elif row.provider == "sarvam" and row.purpose == Purpose.STT:
            values.update(
                stt_provider="sarvam",
                sarvam_stt_key=SecretStr(secrets["api_key"]),
                sarvam_stt_model=config["model"],
                sarvam_stream_type=config["stream_type"],
            )
        elif row.provider == "sarvam":
            values.update(
                tts_provider="sarvam",
                sarvam_tts_key=SecretStr(secrets["api_key"]),
                sarvam_tts_model=config["model"],
                sarvam_speaker=config["speaker"],
                sarvam_voices={
                    "hi": config["speaker_hindi"],
                    "gu": config["speaker_gujarati"],
                    "en": config["speaker_english"],
                },
            )
        elif row.provider == "elevenlabs":
            values.update(
                tts_provider="elevenlabs",
                elevenlabs_api_key=SecretStr(secrets["api_key"]),
                elevenlabs_model_id=config["model_id"],
                elevenlabs_voice_id=config["voice_id"],
            )
        elif row.provider == "azure_speech":
            if row.purpose == Purpose.STT:
                values.update(
                    stt_provider="azure",
                    azure_stt_key=SecretStr(secrets["speech_key"]),
                    azure_stt_region=config["region"],
                    azure_speech_language=config["language"],
                )
            else:
                values.update(
                    tts_provider="azure",
                    azure_tts_key=SecretStr(secrets["speech_key"]),
                    azure_tts_region=config["region"],
                    azure_speech_voice=config["voice"],
                    azure_hindi_voice=config["hindi_voice"],
                )
        elif row.provider in {"gemini", "openrouter"}:
            # Every job's model and the text model's thinking level.
            values.update(llm_provider=row.provider, llm_config=dict(config))
            values[f"{row.provider}_api_key"] = SecretStr(secrets["api_key"])
        elif row.provider == "smtp":
            values.update(
                smtp_host=config["host"],
                smtp_port=config["port"],
                smtp_tls_mode=config["tls_mode"],
                smtp_username=config["username"],
                smtp_password=SecretStr(secrets.get("password", "")),
                smtp_sender=config["sender"],
            )
    if product is not None:
        values.update(
            platform_session_minutes=product.session_minutes,
            product_name=product.product_name,
            support_email=product.support_email,
            speaking_speed=product.speaking_speed,
        )
    return values


async def refresh_platform(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> bool:
    """Copy what the administrators saved onto this process's settings.

    Returns False, leaving the settings as they were, when the database cannot be read;
    the next refresh tries again.
    """
    try:
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(PlatformConnection).where(PlatformConnection.status == "verified")
                )
            ).all()
            product = await session.get(PlatformSettings, 1)
            session.expunge_all()
    except (SQLAlchemyError, OSError, TimeoutError) as failure:
        logger.warning(
            "platform_settings_unavailable", extra={"error_type": type(failure).__name__}
        )
        return False
    connections: list[tuple[PlatformConnection, dict[str, str]]] = []
    if rows:
        cipher = SecretCipher(
            settings.secret_encryption_keys, settings.secret_encryption_active_key
        )
        for row in rows:
            try:
                plain = cipher.decrypt(
                    row.key_id, row.ciphertext, _context(row.purpose, row.provider)
                )
            except (ValueError, InvalidTag):
                # A secret this keyring cannot open is treated as not configured.
                logger.error("platform_connection_unreadable", extra={"purpose": row.purpose})
                continue
            connections.append((row, json.loads(plain)))
    # A value passed in code (a test, an embedding program) is left alone. Assigning
    # past pydantic keeps these out of model_fields_set, which is how the next refresh
    # still tells the two apart.
    explicit = settings.model_fields_set & PLATFORM_MANAGED
    for name, value in platform_values(connections, product).items():
        if name not in explicit:
            object.__setattr__(settings, name, value)
    return True


async def keep_platform_fresh(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Pick up what another process saved. Runs until cancelled."""
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        await refresh_platform(settings, sessions)


class PlatformConnectionService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        verifier: ProviderVerifier | None = None,
    ) -> None:
        self._sessions, self._settings = sessions, settings
        self._verifier = verifier or HttpProviderVerifier()

    def _cipher(self) -> SecretCipher:
        return SecretCipher(
            self._settings.secret_encryption_keys, self._settings.secret_encryption_active_key
        )

    @staticmethod
    def _audit(
        session: AsyncSession, context: PlatformContext, action: str, row: PlatformConnection | None
    ) -> None:
        session.add(
            PlatformAuditEvent(
                user_id=context.user_id,
                action=action,
                request_id=context.request_id,
                resource_id=row.id if row else None,
            )
        )

    async def _row(
        self, session: AsyncSession, purpose: Purpose, *, lock: bool = False
    ) -> PlatformConnection | None:
        query = select(PlatformConnection).where(PlatformConnection.purpose == purpose.value)
        return await session.scalar(query.with_for_update() if lock else query)

    async def list(self, context: PlatformContext) -> list[ConnectionView]:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            rows = {row.purpose: row for row in await session.scalars(select(PlatformConnection))}
            return [_view(purpose, rows.get(purpose.value)) for purpose in PLATFORM_PURPOSES]

    async def save(
        self, context: PlatformContext, purpose: Purpose, command: SaveConnection
    ) -> ConnectionView:
        """Save the details, then check them with the provider straight away."""
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        if purpose not in PLATFORM_PURPOSES:
            raise NotFoundError()
        cipher = self._cipher()
        try:
            spec = platform_spec(purpose.value, command.provider)
            config = validate_config(spec, command.config)
            secrets = validate_secrets(spec, command.secrets) if command.secrets else None
            account = command.account_identifier
            if spec.account_pattern is not None:
                import re

                if account is None or not re.fullmatch(spec.account_pattern, account):
                    raise InvalidIntegrationError("Invalid account identifier")
            else:
                account = None
        except InvalidIntegrationError as error:
            if error.field:
                raise ValidationFailedError([(error.field, str(error))]) from None
            raise InvalidConnectionError() from None
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await self._row(session, purpose, lock=True)
            if secrets is None:
                # Keeping the saved secret only makes sense for the same provider.
                if row is None or row.provider != command.provider:
                    raise InvalidConnectionError()
            else:
                key_id, ciphertext = cipher.encrypt(
                    json.dumps(secrets).encode(), _context(purpose.value, command.provider)
                )
                suffix = secrets.get(spec.suffix_field, "")[-4:]
                if row is None:
                    row = PlatformConnection(
                        id=uuid4(),
                        purpose=purpose.value,
                        provider=command.provider,
                        key_id=key_id,
                        ciphertext=ciphertext,
                    )
                    session.add(row)
                row.key_id, row.ciphertext, row.display_suffix = key_id, ciphertext, suffix
            row.provider, row.account_identifier, row.config = command.provider, account, config
            row.status, row.error_code, row.updated_by = "saved_unverified", None, context.user_id
            self._audit(session, context, f"platform_{purpose.value}_connection_saved", row)
        return await self.verify(context, purpose)

    async def verify(self, context: PlatformContext, purpose: Purpose) -> ConnectionView:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        cipher = self._cipher()
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await self._row(session, purpose)
            if row is None:
                raise NotFoundError()
            provider, account, config = row.provider, row.account_identifier, dict(row.config)
            secrets: dict[str, str] = json.loads(
                cipher.decrypt(row.key_id, row.ciphertext, _context(purpose.value, provider))
            )
        # The provider is asked outside any transaction.
        if purpose in CALENDAR_APPS:
            result = Verification("verified", "ok")
        elif purpose == Purpose.EMAIL:
            result = await asyncio.to_thread(_check_smtp, config, secrets.get("password", ""))
        else:
            result = await self._verifier.verify(purpose.value, provider, account, config, secrets)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await self._row(session, purpose, lock=True)
            if row is None:
                raise NotFoundError()
            if result.outcome == "unavailable":
                # Not proof either way: a working connection keeps working.
                row.error_code = result.code
            else:
                row.status = "verified" if result.outcome == "verified" else "invalid"
                row.error_code = None if result.outcome == "verified" else result.code
            row.last_checked_at = datetime.now(UTC)
            self._audit(session, context, f"platform_{purpose.value}_connection_checked", row)
            await session.flush()
            await session.refresh(row)
            view = _view(purpose, row)
        await refresh_platform(self._settings, self._sessions)
        return view

    async def remove(self, context: PlatformContext, purpose: Purpose) -> None:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await self._row(session, purpose, lock=True)
            if row is None:
                raise NotFoundError()
            self._audit(session, context, f"platform_{purpose.value}_connection_removed", row)
            await session.delete(row)
        await refresh_platform(self._settings, self._sessions)

    async def send_test_mail(self, context: PlatformContext, command: TestMail) -> None:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        await refresh_platform(self._settings, self._sessions)
        if not self._settings.mail_enabled:
            raise MailNotReadyError()
        payload = {
            "to": command.to,
            "subject": f"{self._settings.product_name}: test email",
            "body": (
                f"This is a test message from {self._settings.product_name}.\n\n"
                "If you are reading it, the email server is set up correctly and "
                "invitations and password reset links will be delivered."
            ),
        }
        try:
            await asyncio.to_thread(send_smtp, self._settings, payload, str(uuid4()))
        except (smtplib.SMTPException, OSError):
            raise TestMailFailedError() from None
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            self._audit(session, context, "platform_test_mail_sent", None)

    async def product(self, context: PlatformContext) -> ProductSettings:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await session.get(PlatformSettings, 1)
            if row is None:
                raise NotFoundError()
            return ProductSettings.model_validate(row)

    async def save_product(
        self, context: PlatformContext, command: ProductSettings
    ) -> ProductSettings:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await session.get(PlatformSettings, 1, with_for_update=True)
            if row is None:
                raise NotFoundError()
            for field, value in command.model_dump().items():
                setattr(row, field, value)
            self._audit(session, context, "platform_settings_saved", None)
            await session.flush()
            view = ProductSettings.model_validate(row)
        await refresh_platform(self._settings, self._sessions)
        return view
