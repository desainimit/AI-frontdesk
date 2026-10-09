import asyncio
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.exc import SQLAlchemyError

from app.agents.appointment_tools import appointment_tools
from app.agents.context import ALL_TOOLS, AgentContext
from app.agents.providers.base import LLMProvider
from app.agents.runtime import AgentRuntime
from app.api.platform import PlatformIdentity
from app.api.tenancy import Context, DatabaseDependency, Limit, Offset, Since, Until
from app.audit.events import AuditAction
from app.calls.access import CallPatientAccess, IssuePatientAccess, PatientAccessCode
from app.calls.service import (
    CallBinding,
    CallContext,
    CallService,
    CallView,
    PhoneCommand,
    PhoneView,
)
from app.config.settings import Settings
from app.conversation import ConversationStateManager, build_conversation_store
from app.conversation.locales import LOCALES, LanguagePolicy, base_language
from app.conversation.state import CallStatus, ConversationState, SafetyDecision
from app.conversation.store import ConversationStateStore
from app.database.session import Database
from app.errors import ApplicationError, NotFoundError
from app.integrations.resolver import (
    IntegrationUnavailableError,
    ResolvedIntegration,
    TenantIntegrationResolver,
)
from app.postcall.schemas import PostCallInput
from app.safety.classifier import LLMSafetyClassifier
from app.safety.engine import SafetyEngine
from app.safety.messages import transfer_failed_message
from app.safety.transfer import TransferError, transfer_call
from app.security.rate_limit import enforce_rate_limit
from app.voice.circuit import CircuitRegistry
from app.voice.factory import (
    follows_caller_language,
    platform_stt,
    platform_tts,
    stt_from_connection,
    tts_from_connection,
)
from app.voice.metrics import VoiceMetrics
from app.voice.pipeline import VoicePipeline
from app.voice.processors.interruption import InterruptionConfig
from app.voice.stt.base import SpeechToTextProvider, TranscriptionResult
from app.voice.tts.base import TextToSpeechProvider
from app.voice.twilio import (
    TwilioCredentials,
    decode_audio,
    matching_credentials,
    parse_message,
    stream_token_valid,
    twiml,
)

router = APIRouter(tags=["telephony"])
logger = logging.getLogger(__name__)


@router.post("/api/v1/platform/phone-numbers", status_code=201)
async def assign_phone(
    command: PhoneCommand, context: PlatformIdentity, database: DatabaseDependency
) -> PhoneView:
    return await CallService(database.session_factory).assign(context, command)


@router.get("/api/v1/platform/phone-numbers")
async def list_phones(
    context: PlatformIdentity, database: DatabaseDependency, limit: Limit = 50, offset: Offset = 0
) -> list[PhoneView]:
    return await CallService(database.session_factory).phones(context, limit, offset)


@router.get("/api/v1/calls")
async def list_calls(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    status: Literal["received", "active", "completed", "disconnected", "failed"] | None = None,
    created_from: Since = None,
    created_to: Until = None,
) -> list[CallView]:
    return await CallService(database.session_factory).list(
        context,
        limit,
        offset,
        status=status,
        created_from=created_from,
        created_to=created_to,
    )


@router.get("/api/v1/calls/{call_id}")
async def get_call(call_id: UUID, context: Context, database: DatabaseDependency) -> CallView:
    rows = await CallService(database.session_factory).list(context, 1, 0, call_id)
    if not rows:
        raise NotFoundError()
    return rows[0]


@router.post("/api/v1/calls/{call_id}/patient-access", status_code=201)
async def issue_patient_access(
    call_id: UUID, command: IssuePatientAccess, context: Context, database: DatabaseDependency
) -> PatientAccessCode:
    return await CallPatientAccess(database.session_factory).issue(context, call_id, command)


def tenant_resolver(app: FastAPI) -> TenantIntegrationResolver | None:
    """The connection resolver; absent only when the app runs without a real database."""
    resolver = getattr(app.state, "integration_resolver", None)
    return resolver if isinstance(resolver, TenantIntegrationResolver) else None


def circuit_registry(app: FastAPI) -> CircuitRegistry:
    registry = getattr(app.state, "voice_circuits", None)
    if not isinstance(registry, CircuitRegistry):
        registry = CircuitRegistry()
        app.state.voice_circuits = registry
    return registry


def twilio_credentials(connection: ResolvedIntegration) -> TwilioCredentials:
    key_sid = connection.secrets.get("api_key_sid")
    return TwilioCredentials(
        account_sid=connection.account_identifier or "",
        auth_token=connection.secret("auth_token"),
        api_key_sid=key_sid.get_secret_value() if key_sid else None,
        api_key_secret=connection.secrets.get("api_key_secret"),
        integration_id=connection.integration_id,
        version_id=connection.version_id,
    )


async def tenant_candidates(
    app: FastAPI, tenant_id: UUID | None, *, fresh: bool = False
) -> list[TwilioCredentials]:
    """Accounts allowed to open a new call for this tenant: its own connection when it
    has one, otherwise the platform account."""
    settings: Settings = app.state.settings
    platform = [TwilioCredentials.from_settings(settings)] if settings.platform_twilio else []
    resolver = tenant_resolver(app)
    if resolver is None:
        return platform
    if tenant_id is None:
        return []
    connection = await resolver.active(tenant_id, "telephony", fresh=fresh)
    return [twilio_credentials(connection)] if connection is not None else platform


async def call_candidates(
    app: FastAPI, service: CallService, tenant_id: UUID, call_id: UUID
) -> list[TwilioCredentials]:
    """Accounts allowed to continue an existing call: only the one pinned to it."""
    settings: Settings = app.state.settings
    platform = [TwilioCredentials.from_settings(settings)] if settings.platform_twilio else []
    resolver = tenant_resolver(app)
    if resolver is None:
        return platform
    binding = await service.binding(tenant_id, call_id)
    if binding is None:
        return []
    if binding.integration_id is None or binding.credential_version_id is None:
        return platform  # The call was opened on the platform account.
    return [
        twilio_credentials(connection)
        for connection in await resolver.for_call(
            tenant_id, binding.integration_id, binding.credential_version_id
        )
    ]


async def read_form(request: Request) -> dict[str, str]:
    """Parse an unauthenticated webhook body. Nothing here is trusted yet."""
    settings: Settings = request.app.state.settings
    if not settings.telephony_ready:
        raise HTTPException(503, "Telephony is not configured")
    if tenant_resolver(request.app) is not None:
        # Choosing a candidate account needs a lookup before the signature can be
        # checked, so unauthenticated senders are bounded first.
        await enforce_rate_limit(
            request.app.state.resources.redis,
            enabled=request.app.state.rate_limits,
            scope="twilio_unverified",
            identity=request.client.host if request.client else "unknown",
            limit=settings.webhook_requests_per_call_per_minute * 20,
        )
    if request.url.query or request.headers.get("content-type", "").split(";")[0] != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(400, "Invalid webhook format")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            raise HTTPException(413, "Webhook is too large")
    try:
        pairs = parse_qsl(
            body.decode("utf-8"), keep_blank_values=True, max_num_fields=100, strict_parsing=True
        )
    except (UnicodeError, ValueError):
        raise HTTPException(400, "Invalid webhook format") from None
    params = dict(pairs)
    if len(params) != len(pairs):
        raise HTTPException(400, "Duplicate webhook fields")
    return params


async def authenticate_form(
    request: Request, params: dict[str, str], candidates: list[TwilioCredentials]
) -> TwilioCredentials:
    """Verify the signature and account against the candidates. The tenant selected to
    find those candidates is established only when this succeeds."""
    settings: Settings = request.app.state.settings
    credentials = matching_credentials(
        settings,
        candidates,
        request.url.path,
        request.headers.get("x-twilio-signature", ""),
        params,
    )
    if credentials is None:
        raise HTTPException(403, "Invalid provider signature")
    if params.get("AccountSid") != credentials.account_sid:
        raise HTTPException(403, "Invalid provider account")
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", params.get("CallSid", "")):
        raise HTTPException(400, "Invalid call identifier")
    await enforce_rate_limit(
        request.app.state.resources.redis,
        enabled=request.app.state.rate_limits,
        scope="twilio_webhook",
        identity=f"{params['AccountSid']}:{params['CallSid']}",
        limit=settings.webhook_requests_per_call_per_minute,
    )
    return credentials


async def authenticate_for_number(
    request: Request, service: CallService, params: dict[str, str]
) -> TwilioCredentials:
    """Authenticate a webhook routed by destination number."""
    app: FastAPI = request.app
    tenant_id: UUID | None = None
    if tenant_resolver(app) is not None and re.fullmatch(
        r"\+[1-9][0-9]{7,14}", params.get("To", "")
    ):
        tenant_id = await service.tenant_for_number(params["To"])
    try:
        return await authenticate_form(request, params, await tenant_candidates(app, tenant_id))
    except HTTPException as error:
        if error.status_code != 403 or tenant_id is None:
            raise
    # A credential rotated on another replica may not be in this process's cache yet.
    return await authenticate_form(
        request, params, await tenant_candidates(app, tenant_id, fresh=True)
    )


@router.post("/webhooks/v1/twilio/incoming", response_class=Response)
async def incoming(request: Request, database: DatabaseDependency) -> Response:
    params = await read_form(request)
    service = CallService(database.session_factory)
    credentials = await authenticate_for_number(request, service, params)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", params.get("To", "")):
        raise HTTPException(400, "Invalid destination")
    context = await service.receive(
        params["To"],
        params["CallSid"],
        UUID(request.state.request_id),
        CallBinding(credentials.integration_id, credentials.version_id),
    )
    return Response(
        twiml(request.app.state.settings, context, credentials),
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/webhooks/v1/twilio/{tenant_id}/{call_id}/status", status_code=204)
async def stream_status(
    tenant_id: UUID, call_id: UUID, request: Request, database: DatabaseDependency
) -> Response:
    params = await read_form(request)
    service = CallService(database.session_factory)
    await authenticate_form(
        request, params, await call_candidates(request.app, service, tenant_id, call_id)
    )
    event = params.get("StreamEvent")
    if event not in {"stream-started", "stream-stopped", "stream-error"}:
        raise HTTPException(400, "Invalid stream event")
    if event != "stream-started":
        await service.finish(
            CallContext(tenant_id, call_id, params["CallSid"], UUID(request.state.request_id)),
            "failed" if event == "stream-error" else "completed",
            event,
        )
    return Response(status_code=204)


@router.post("/webhooks/v1/twilio/status", status_code=204)
async def call_status(request: Request, database: DatabaseDependency) -> Response:
    params = await read_form(request)
    service = CallService(database.session_factory)
    credentials = await authenticate_for_number(request, service, params)
    status = params.get("CallStatus", "")
    if status not in {
        "queued",
        "ringing",
        "in-progress",
        "completed",
        "busy",
        "failed",
        "no-answer",
        "canceled",
    }:
        raise HTTPException(400, "Invalid call status")
    if status in {"completed", "busy", "failed", "no-answer", "canceled"}:
        await service.provider_status(
            params.get("To", ""),
            params["CallSid"],
            status,
            UUID(request.state.request_id),
            credentials.integration_id,
        )
    return Response(status_code=204)


@dataclass
class CallProviders:
    """Speech and model providers for one call: the tenant's own connection for each,
    otherwise the platform account."""

    stt_connection: ResolvedIntegration | None
    tts: TextToSpeechProvider
    llm: LLMProvider
    owned_llm: LLMProvider | None
    multilingual: bool

    def speech_to_text(
        self, app: FastAPI, settings: Settings, speech_locale: Callable[[], str]
    ) -> SpeechToTextProvider:
        locale = speech_locale if self.multilingual else None
        if self.stt_connection is not None:
            return stt_from_connection(settings, self.stt_connection, locale)
        provider = app.state.stt_provider or platform_stt(settings, locale)
        if provider is None:
            raise IntegrationUnavailableError()
        return provider


async def resolve_call_providers(
    app: FastAPI, settings: Settings, tenant_id: UUID
) -> CallProviders:
    """Every provider this call needs, checked before the caller hears anything.

    A missing provider fails the call; nothing falls back to a test or echo mode.
    """
    resolver = tenant_resolver(app)
    stt_connection = tts_connection = llm_connection = None
    if resolver is not None:
        stt_connection = await resolver.active(tenant_id, "stt")
        tts_connection = await resolver.active(tenant_id, "tts")
        llm_connection = await resolver.active(tenant_id, "llm")
    if stt_connection is None and app.state.stt_provider is None and platform_stt(settings) is None:
        raise IntegrationUnavailableError()
    tts: TextToSpeechProvider | None = (
        tts_from_connection(settings, tts_connection)
        if tts_connection is not None
        else app.state.tts_provider or platform_tts(settings)
    )
    if tts is None:
        raise IntegrationUnavailableError()
    llm: LLMProvider | None = app.state.llm_provider
    owned_llm: LLMProvider | None = None
    if llm_connection is not None:
        from app.agents.factory import llm_from_connection

        owned_llm = llm = llm_from_connection(
            settings,
            llm_connection.provider,
            llm_connection.secret("api_key"),
            dict(llm_connection.config),
        )
    if llm is None:
        raise IntegrationUnavailableError()
    return CallProviders(
        stt_connection, tts, llm, owned_llm, follows_caller_language(settings, stt_connection)
    )


async def run_media(
    socket: WebSocket,
    service: CallService,
    settings: Settings,
    tenant_id: UUID,
    call_id: UUID,
    credentials: TwilioCredentials | None = None,
) -> None:
    # The account that authenticated this stream: the tenant's own, or the platform's.
    account = credentials or TwilioCredentials.from_settings(settings)
    owned_llm: LLMProvider | None = None
    context: CallContext | None = None
    conversation_state: ConversationState | None = None
    conversation_store: ConversationStateStore | None = None
    received = sent = 0
    status, reason = "disconnected", "socket_disconnected"
    try:
        async with asyncio.timeout(10):
            connected = parse_message(await socket.receive_text())
            if connected.get("event") != "connected" or connected.get("protocol") != "Call":
                raise ValueError("Invalid connection")
            message = parse_message(await socket.receive_text())
            start = message.get("start", {})
            stream_sid = start.get("streamSid", "")
            sid = start.get("callSid", "")
            candidate = CallContext(tenant_id, call_id, sid, uuid4())
            if (
                message.get("event") != "start"
                or not re.fullmatch(r"MZ[0-9a-fA-F]{32}", stream_sid)
                or not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid)
                or message.get("streamSid") != stream_sid
                or start.get("accountSid") != account.account_sid
                or start.get("mediaFormat")
                != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
                or start.get("tracks") != ["inbound"]
                or not stream_token_valid(
                    settings,
                    candidate,
                    start.get("customParameters", {}).get("token", ""),
                    account,
                )
            ):
                raise ValueError("Invalid stream binding")
            await service.start(candidate, stream_sid)
            context = candidate
        telemetry = socket.app.state.telemetry
        redis_dep = getattr(getattr(socket.app.state, "resources", None), "redis", None)
        resolver = tenant_resolver(socket.app)
        providers = await resolve_call_providers(socket.app, settings, tenant_id)
        owned_llm, llm, multilingual = (
            providers.owned_llm,
            providers.llm,
            providers.multilingual,
        )
        tts = providers.tts
        operational = await service.operational_configuration(context)
        configuration_version = operational[0] if operational else None
        configuration = operational[1] if operational else None
        conv_store = socket.app.state.conversation_store or build_conversation_store(
            settings, redis_dep
        )
        conversation_store = conv_store
        initial_conv_state = ConversationStateManager.create_initial_state(
            call_id=call_id,
            tenant_id=tenant_id,
            telephony_call_id=sid,
            caller_phone=getattr(context, "caller_phone", None),
            default_language=settings.conversation_default_language,
            confirmation_timeout_seconds=settings.conversation_confirmation_timeout_seconds,
            language_policy=(
                configuration.languages
                if configuration is not None
                else await service.language_policy(context, settings.conversation_default_language)
                if multilingual
                else LanguagePolicy(
                    default_language=settings.conversation_default_language,
                    enabled_languages=[settings.conversation_default_language],
                    auto_detect=False,
                )
            ),
        )

        def speech_locale() -> str:
            return LOCALES[base_language(initial_conv_state.language)].speech_locale

        stt = providers.speech_to_text(socket.app, settings, speech_locale)
        conversation_state = initial_conv_state
        initial_conv_state.call_state.stream_sid = stream_sid
        await conv_store.save(
            initial_conv_state, ttl_seconds=settings.conversation_state_ttl_seconds
        )
        transfer_number = (
            configuration.voice.transfer_number
            if configuration is not None
            else await service.transfer_number(context)
        )
        # Adds one model request per caller turn; the deterministic rules always run.
        classifier = LLMSafetyClassifier(llm) if settings.safety_semantic_enabled else None
        safety_engine = SafetyEngine(
            classifier,
            transfer_available=transfer_number is not None,
            classifier_timeout_seconds=settings.safety_classifier_timeout_seconds,
        )
        voice_metrics = VoiceMetrics(telemetry)
        registry, appointment_flow = appointment_tools(
            service.sessions, settings.agent_tool_timeout_seconds, settings, resolver
        )
        agent = AgentRuntime(
            llm,
            registry,
            appointment_flow=appointment_flow,
            safety_engine=safety_engine,
            max_steps=configuration.agent.max_steps if configuration else settings.agent_max_steps,
            max_tool_calls=configuration.agent.max_tool_calls
            if configuration
            else settings.agent_max_tool_calls,
            turn_timeout_seconds=settings.agent_turn_timeout_seconds,
            metrics=voice_metrics,
        )
        agent_context = AgentContext(
            context,
            frozenset(configuration.agent.allowed_tools) if configuration else ALL_TOOLS,
            business_name=configuration.business.name if configuration else "",
            instructions=configuration.agent.instructions if configuration else "",
            tone=configuration.agent.tone if configuration else "professional",
            configuration_version=configuration_version,
            configuration=configuration,
        )

        async def send_frame(msg: dict[str, Any]) -> None:
            async with asyncio.timeout(5):
                await socket.send_json(msg)

        async def on_transcript_cb(result: TranscriptionResult) -> None:
            nonlocal status, reason
            if not result.is_final or not result.text.strip():
                return
            reply = await agent.respond(agent_context, initial_conv_state, result.text)
            response = reply.response
            safety = initial_conv_state.safety_state
            if safety.decision == SafetyDecision.EMERGENCY_PROTOCOL and not safety.event_recorded:
                safety.event_recorded = True
                voice_metrics.record_emergency(safety.source or "rule")
                try:
                    await service.record_safety_event(context, AuditAction.EMERGENCY_DETECTED)
                except Exception:
                    logger.warning("safety_audit_failed", extra={"call_id": str(call_id)})
            if safety.decision == SafetyDecision.ESCALATE and not safety.event_recorded:
                safety.event_recorded = True
                voice_metrics.record_human_transfer("unavailable")
                try:
                    await service.record_safety_event(context, AuditAction.HUMAN_TRANSFER_REQUESTED)
                except Exception:
                    logger.warning("safety_audit_failed", extra={"call_id": str(call_id)})
            if safety.decision == SafetyDecision.TRANSFER:
                if safety.transfer_attempted:
                    return
                safety.transfer_attempted = True
                try:
                    await service.record_safety_event(context, AuditAction.HUMAN_TRANSFER_REQUESTED)
                except Exception:
                    logger.warning("safety_audit_failed", extra={"call_id": str(call_id)})
                await pipeline.speak(response)
                if transfer_number is None:
                    return
                try:
                    await transfer_call(settings, context, transfer_number, credentials=account)
                    safety.transfer_confirmed = True
                    status, reason = "completed", "human_transfer"
                    voice_metrics.record_human_transfer("redirected")
                    try:
                        await service.record_safety_event(
                            context, AuditAction.HUMAN_TRANSFER_REDIRECTED
                        )
                    except Exception:
                        logger.warning("safety_audit_failed", extra={"call_id": str(call_id)})
                except TransferError:
                    voice_metrics.record_human_transfer("failed")
                    safety.decision = SafetyDecision.ESCALATE
                    await pipeline.speak(transfer_failed_message(initial_conv_state.language))
                    try:
                        await service.record_safety_event(
                            context, AuditAction.HUMAN_TRANSFER_FAILED
                        )
                    except Exception:
                        logger.warning("safety_audit_failed", extra={"call_id": str(call_id)})
                return
            await pipeline.speak(response)

        interruption_config = (
            InterruptionConfig(
                energy_threshold=settings.barge_in_energy_threshold,
                consecutive_frames_required=settings.barge_in_consecutive_frames,
            )
            if settings.barge_in_enabled
            else None
        )

        pipeline = VoicePipeline(
            stream_sid=stream_sid,
            metrics=voice_metrics,
            stt_provider=stt,
            tts_provider=tts,
            send_frame=send_frame,
            on_transcript=on_transcript_cb,
            interruption_config=interruption_config,
            conversation_state=initial_conv_state,
            conversation_store=conv_store,
            conversation_ttl_seconds=settings.conversation_state_ttl_seconds,
            language_threshold=settings.conversation_language_confidence_threshold,
            multilingual_enabled=multilingual,
            barge_in_enabled=settings.barge_in_enabled,
        )
        previous_sequence = int(message["sequenceNumber"])
        try:
            await pipeline.start()
            if configuration is not None:
                await pipeline.speak(configuration.agent.greeting)
            call_seconds = (
                configuration.voice.max_duration_minutes * 60
                if configuration
                else settings.telephony_max_call_seconds
            )
            async with asyncio.timeout(call_seconds):
                while True:
                    async with asyncio.timeout(settings.telephony_idle_seconds):
                        message = parse_message(await socket.receive_text())
                    sequence = int(message["sequenceNumber"])
                    if message.get("streamSid") != stream_sid or sequence <= previous_sequence:
                        raise ValueError("Invalid stream sequence")
                    previous_sequence = sequence
                    event = message["event"]
                    if event == "stop":
                        stop = message.get("stop", {})
                        if stop.get("callSid") != context.provider_call_id or (
                            stop.get("accountSid") != account.account_sid
                        ):
                            raise ValueError("Invalid stop binding")
                        status = "completed"
                        reason = (
                            "human_transfer"
                            if initial_conv_state.safety_state.transfer_confirmed
                            else "stream_stopped"
                        )
                        break
                    if event == "mark":
                        pipeline.acknowledge_playback(message.get("mark", {}).get("name", ""))
                        continue
                    if event == "dtmf":
                        continue
                    if event != "media" or message["media"].get("track") != "inbound":
                        raise ValueError("Invalid audio event")
                    audio = decode_audio(message["media"]["payload"])
                    await pipeline.process_inbound_audio(audio)
        finally:
            received = pipeline.received_frames
            sent = pipeline.sent_frames
            await pipeline.close()
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        reason = "server_shutdown"
        raise
    except TimeoutError:
        status, reason = "disconnected", "stream_timeout"
    except IntegrationUnavailableError:
        status, reason = "failed", "integration_unavailable"
        logger.error("call_integration_unavailable", extra={"call_id": str(call_id)})
    except (ValueError, KeyError, TypeError, AttributeError, ApplicationError):
        status, reason = "failed", "invalid_stream"
    except Exception:
        status, reason = "failed", "pipeline_error"
        logger.error("call_pipeline_failed", extra={"call_id": str(call_id)})
    finally:
        if owned_llm is not None:
            with suppress(Exception):
                await owned_llm.close()
        if conversation_state is not None and conversation_store is not None:
            conversation_state.call_state.status = {
                "completed": CallStatus.COMPLETED,
                "failed": CallStatus.FAILED,
                "disconnected": CallStatus.DISCONNECTED,
            }[status]
            conversation_state.call_state.ended_at = datetime.now(UTC)
            try:
                async with asyncio.timeout(5):
                    await conversation_store.save(
                        conversation_state, settings.conversation_state_ttl_seconds
                    )
            except Exception:
                logger.error("conversation_cleanup_failed", extra={"call_id": str(call_id)})
        if context is not None:
            post_call_input = None
            if conversation_state is not None:
                try:
                    post_call_input = PostCallInput.capture(conversation_state)
                except ValueError:
                    logger.error("post_call_snapshot_rejected", extra={"call_id": str(call_id)})
            try:
                async with asyncio.timeout(10):
                    await asyncio.shield(
                        service.finish(
                            context,
                            status,
                            reason,
                            received,
                            sent,
                            post_call_input=post_call_input,
                        )
                    )
            except (SQLAlchemyError, TimeoutError, ApplicationError):
                logger.error("call_cleanup_failed", extra={"call_id": str(call_id)})
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close(code=1000 if status == "completed" else 1008)


@router.websocket("/webhooks/v1/twilio/{tenant_id}/{call_id}/media")
async def media(socket: WebSocket, tenant_id: UUID, call_id: UUID) -> None:
    settings: Settings = socket.app.state.settings
    database = socket.app.state.resources.database
    if not isinstance(database, Database):
        await socket.close(code=1011)
        return
    service = CallService(database.session_factory)
    signature = socket.headers.get("x-twilio-signature", "")
    credentials: TwilioCredentials | None = None
    if not socket.url.query and signature and settings.telephony_ready:
        try:
            # The path only selects the call; its stored binding decides which account
            # may sign, so a number reassigned later cannot adopt this stream.
            credentials = matching_credentials(
                settings,
                await call_candidates(socket.app, service, tenant_id, call_id),
                socket.url.path,
                signature,
                websocket=True,
            )
        except (SQLAlchemyError, ApplicationError):
            credentials = None
    if credentials is None:
        await socket.close(code=1008)
        return
    await socket.accept()
    await run_media(socket, service, settings, tenant_id, call_id, credentials)
