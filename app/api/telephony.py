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

from app.ai.engine import Engine
from app.ai.llm.base import LLMProvider
from app.ai.llm.factory import build_llm_provider, llm_from_connection
from app.ai.llm.gateway import ModelGateway, TurnLimits, breaker_for
from app.ai.memory import CallerHistory
from app.ai.runtime import AssistantRuntime
from app.ai.session import Session
from app.ai.tools.toolbox import build_tools
from app.api.tenancy import Context, Cursor, DatabaseDependency, Limit, Since, Until
from app.calls.access import CallPatientAccess, IssuePatientAccess, PatientAccessCode
from app.calls.service import (
    CallBinding,
    CallContext,
    CallService,
    CallView,
)
from app.conversation import ConversationStateManager, build_conversation_store
from app.conversation.locales import LOCALES, LanguagePolicy, base_language
from app.conversation.state import CallStatus, ConversationState
from app.conversation.store import ConversationStateStore
from app.core.database.pages import Page, decode_cursor, page
from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.core.errors import ApplicationError, NotFoundError
from app.core.security.rate_limit import enforce_rate_limit
from app.core.settings import Settings
from app.integrations.resolver import (
    IntegrationUnavailableError,
    ResolvedIntegration,
    TenantIntegrationResolver,
)
from app.messaging.recovery import answer_soon
from app.messaging.service import InboundTexts
from app.postcall.schemas import PostCallInput
from app.safety.classifier import LLMSafetyClassifier
from app.safety.engine import SafetyEngine
from app.tickets.service import open_ticket
from app.voice.circuit import CircuitRegistry
from app.voice.factory import (
    follows_caller_language,
    platform_stt,
    platform_tts,
    stt_from_connection,
    tts_from_connection,
    validate_gujarati_voice,
)
from app.voice.keyterms import call_keyterms
from app.voice.metrics import VoiceMetrics
from app.voice.phrases import MemoryPhraseCache, PhraseCache
from app.voice.pipeline import PhraseStore, VoicePipeline
from app.voice.processors.interruption import InterruptionConfig
from app.voice.receptionist import CallReceptionist
from app.voice.stt.base import SpeechToTextProvider
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

WEBHOOK_REQUESTS_PER_CALL_PER_MINUTE = 60
TEXTS_PER_SENDER_PER_MINUTE = 20
E164 = r"\+[1-9][0-9]{7,14}"
# A stream with no audio for this long is closed; a call never runs longer than the cap.
IDLE_SECONDS = 30
MAX_CALL_SECONDS = 600
CONVERSATION_TTL_SECONDS = 3600


@router.get("/api/v1/calls")
async def list_calls(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Literal["received", "active", "completed", "disconnected", "failed"] | None = None,
    created_from: Since = None,
    created_to: Until = None,
) -> Page[CallView]:
    offset = decode_cursor(cursor)
    rows = await CallService(database.session_factory).list(
        context,
        limit + 1,
        offset,
        status=status,
        created_from=created_from,
        created_to=created_to,
    )
    return page(rows, limit, offset)


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


def turn_limits(app: FastAPI) -> TurnLimits:
    limits = getattr(app.state, "turn_limits", None)
    if not isinstance(limits, TurnLimits):
        limits = app.state.turn_limits = TurnLimits()
    return limits


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
            limit=WEBHOOK_REQUESTS_PER_CALL_PER_MINUTE * 20,
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
    request: Request,
    params: dict[str, str],
    candidates: list[TwilioCredentials],
    *,
    text: bool = False,
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
    if text:
        if not re.fullmatch(r"[SM]M[0-9a-fA-F]{32}", params.get("MessageSid", "")):
            raise HTTPException(400, "Invalid message identifier")
        # One sender cannot flood a clinic's thread, however many messages they send.
        subject, limit = params.get("From", ""), TEXTS_PER_SENDER_PER_MINUTE
    else:
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", params.get("CallSid", "")):
            raise HTTPException(400, "Invalid call identifier")
        subject, limit = params["CallSid"], WEBHOOK_REQUESTS_PER_CALL_PER_MINUTE
    await enforce_rate_limit(
        request.app.state.resources.redis,
        enabled=request.app.state.rate_limits,
        scope="twilio_text" if text else "twilio_webhook",
        identity=f"{params['AccountSid']}:{subject}",
        limit=limit,
    )
    return credentials


async def authenticate_for_number(
    request: Request, service: CallService, params: dict[str, str], *, text: bool = False
) -> TwilioCredentials:
    """Authenticate a webhook routed by destination number."""
    app: FastAPI = request.app
    tenant_id: UUID | None = None
    if tenant_resolver(app) is not None and re.fullmatch(
        r"\+[1-9][0-9]{7,14}", params.get("To", "")
    ):
        tenant_id = await service.tenant_for_number(params["To"])
    try:
        return await authenticate_form(
            request, params, await tenant_candidates(app, tenant_id), text=text
        )
    except HTTPException as error:
        if error.status_code != 403 or tenant_id is None:
            raise
    # A credential rotated on another replica may not be in this process's cache yet.
    return await authenticate_form(
        request, params, await tenant_candidates(app, tenant_id, fresh=True), text=text
    )


@router.post("/webhooks/v1/twilio/incoming", response_class=Response)
async def incoming(request: Request, database: DatabaseDependency) -> Response:
    params = await read_form(request)
    service = CallService(database.session_factory)
    credentials = await authenticate_for_number(request, service, params)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", params.get("To", "")):
        raise HTTPException(400, "Invalid destination")
    caller = params.get("From", "")
    context = await service.receive(
        params["To"],
        params["CallSid"],
        UUID(request.state.request_id),
        CallBinding(credentials.integration_id, credentials.version_id),
        # A withheld or malformed caller ID is simply not kept.
        caller if re.fullmatch(r"\+[1-9][0-9]{7,14}", caller) else None,
    )
    return Response(
        twiml(request.app.state.settings, context, credentials),
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/webhooks/v1/twilio/sms", response_class=Response)
async def incoming_text(request: Request, database: DatabaseDependency) -> Response:
    """A text message to one of a clinic's numbers. It is kept at once; the reply is
    written afterwards and sent like any other message."""
    params = await read_form(request)
    service = CallService(database.session_factory)
    await authenticate_for_number(request, service, params, text=True)
    to, sender = params.get("To", ""), params.get("From", "")
    if not re.fullmatch(E164, to) or not re.fullmatch(E164, sender):
        raise HTTPException(400, "Invalid number")
    tenant_id = await service.tenant_for_number(to)
    if tenant_id is None:
        raise HTTPException(404, "Unknown number")
    received = await InboundTexts(database.session_factory).receive(
        tenant_id, to, sender, params.get("Body", ""), params["MessageSid"]
    )
    assistant = getattr(request.app.state, "assistant", None)
    if received.answer and isinstance(assistant, AssistantRuntime):
        pending: set[asyncio.Task[None]] = request.app.state.text_turns
        task = asyncio.create_task(answer_soon(assistant, tenant_id, received.conversation_id))
        pending.add(task)
        task.add_done_callback(pending.discard)
    # Nothing is said in the response itself.
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
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
    # The quicker model: the safety check, and one last attempt at a reply.
    light_llm: LLMProvider | None
    owned: tuple[LLMProvider, ...]
    # Which model account this call uses, for its circuit breaker.
    account: str
    multilingual: bool

    def speech_to_text(
        self,
        app: FastAPI,
        settings: Settings,
        speech_locale: Callable[[], str],
        language_policy: LanguagePolicy | None = None,
        keyterms: tuple[str, ...] = (),
    ) -> SpeechToTextProvider:
        locale = speech_locale if self.multilingual else None
        if self.stt_connection is not None:
            return stt_from_connection(
                settings, self.stt_connection, locale, language_policy, keyterms
            )
        provider = app.state.stt_provider or platform_stt(
            settings, locale, language_policy, keyterms
        )
        if provider is None:
            raise IntegrationUnavailableError()
        return provider


async def resolve_call_providers(
    app: FastAPI,
    settings: Settings,
    tenant_id: UUID,
    language_policy: LanguagePolicy | None = None,
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
    if language_policy is not None:
        validate_gujarati_voice(settings, language_policy, stt_connection, tts_connection)
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
    light: LLMProvider | None = None
    owned: tuple[LLMProvider, ...] = ()
    account = "platform"
    if llm_connection is not None:
        key, config = llm_connection.secret("api_key"), dict(llm_connection.config)
        llm = llm_from_connection(llm_connection.provider, key, config)
        light = llm_from_connection(llm_connection.provider, key, config, light=True)
        owned, account = (llm, light), str(llm_connection.integration_id)
    elif llm is None and settings.platform_llm_key.get_secret_value():
        llm, light = build_llm_provider(settings), build_llm_provider(settings, light=True)
        owned = (llm, light)
    if llm is None:
        raise IntegrationUnavailableError()
    return CallProviders(
        stt_connection,
        tts,
        llm,
        light,
        owned,
        account,
        follows_caller_language(settings, stt_connection),
    )


def phrase_cache(app: FastAPI, redis: object, tenant_id: UUID) -> PhraseStore:
    """Where the audio of repeated phrases is kept: Redis, or this process for local runs."""
    client = getattr(redis, "client", None)
    if client is not None:
        return PhraseCache(client, str(tenant_id))
    if not isinstance(getattr(app.state, "phrase_cache", None), MemoryPhraseCache):
        app.state.phrase_cache = MemoryPhraseCache()
    return app.state.phrase_cache


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
    owned: tuple[LLMProvider, ...] = ()
    caller_number: str | None = None
    context: CallContext | None = None
    receptionist: CallReceptionist | None = None
    conversation_state: ConversationState | None = None
    conversation_store: ConversationStateStore | None = None
    received = sent = 0
    call_summary: str | None = None
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
            caller_number = await service.start(candidate, stream_sid)
            context = candidate
        telemetry = socket.app.state.telemetry
        redis_dep = getattr(getattr(socket.app.state, "resources", None), "redis", None)
        resolver = tenant_resolver(socket.app)
        configuration = await service.settings(context)
        providers = await resolve_call_providers(
            socket.app, settings, tenant_id, configuration.receptionist.languages
        )
        owned, llm, multilingual = providers.owned, providers.llm, providers.multilingual
        tts = providers.tts
        conv_store = socket.app.state.conversation_store or build_conversation_store(
            settings, redis_dep
        )
        conversation_store = conv_store
        initial_conv_state = ConversationStateManager.create_initial_state(
            call_id=call_id,
            tenant_id=tenant_id,
            telephony_call_id=sid,
            caller_phone=caller_number,
            language_policy=configuration.receptionist.languages,
        )

        def speech_locale() -> str:
            return LOCALES[base_language(initial_conv_state.language)].speech_locale

        stt = providers.speech_to_text(
            socket.app,
            settings,
            speech_locale,
            configuration.receptionist.languages,
            await call_keyterms(service.sessions, tenant_id, configuration),
        )
        conversation_state = initial_conv_state
        initial_conv_state.call_state.stream_sid = stream_sid
        await conv_store.save(initial_conv_state, ttl_seconds=CONVERSATION_TTL_SECONDS)
        voice_metrics = VoiceMetrics(telemetry)
        registry, flow, identity = build_tools(service.sessions, settings, resolver)
        engine = Engine(
            ModelGateway(
                llm,
                fallback=providers.light_llm,
                breaker=breaker_for(circuit_registry(socket.app), tenant_id, providers.account),
                slot=turn_limits(socket.app).slot(tenant_id),
            ),
            registry,
            flow,
            # The model's own reading of the risk uses the quicker model and runs beside
            # the reply; the rules always run first.
            SafetyEngine(
                LLMSafetyClassifier(providers.light_llm or llm)
                if settings.safety_semantic_enabled
                else None,
                transfer_available=configuration.calls.transfer_number is not None,
            ),
            identity,
            CallerHistory(service.sessions),
        )
        receptionist = CallReceptionist(
            engine=engine,
            session=Session(
                tenant_id,
                call_id,
                "voice",
                context.request_id,
                configuration,
                configuration.receptionist.allowed_tools,
                provider_call_id=sid,
                contact=caller_number,
            ),
            state=initial_conv_state,
            store=conv_store,
            store_ttl_seconds=CONVERSATION_TTL_SECONDS,
            service=service,
            sessions=service.sessions,
            settings=settings,
            account=account,
            metrics=voice_metrics,
            summarizer=providers.light_llm or llm,
        )

        async def send_frame(msg: dict[str, Any]) -> None:
            async with asyncio.timeout(5):
                await socket.send_json(msg)

        pipeline = VoicePipeline(
            stream_sid=stream_sid,
            metrics=voice_metrics,
            stt_provider=stt,
            tts_provider=tts,
            send_frame=send_frame,
            on_transcript=receptionist.reply,
            on_resume=receptionist.resume,
            interruption_config=InterruptionConfig(),
            conversation_state=initial_conv_state,
            conversation_store=conv_store,
            conversation_ttl_seconds=CONVERSATION_TTL_SECONDS,
            multilingual_enabled=multilingual,
            phrases=phrase_cache(socket.app, redis_dep, tenant_id),
            # A clinic's chosen voice is an ElevenLabs voice; another service keeps its own.
            voice_id=(
                configuration.receptionist.voice_id or None
                if tts.name in {"elevenlabs", "language_router"}
                else None
            ),
        )
        receptionist.pipeline = pipeline
        previous_sequence = int(message["sequenceNumber"])
        try:
            await pipeline.start()
            await pipeline.speak(configuration.receptionist.greeting, cache=True)
            async with asyncio.timeout(configuration.calls.max_duration_minutes * 60):
                while True:
                    async with asyncio.timeout(IDLE_SECONDS):
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
                        reason = "human_transfer" if receptionist.transferred else "stream_stopped"
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
            # Written while the model connection is still open; closed below.
            call_summary = await receptionist.call_summary()
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        reason = "server_shutdown"
        raise
    except TimeoutError:
        status, reason = "disconnected", "stream_timeout"
    except IntegrationUnavailableError as error:
        status, reason = "failed", "integration_unavailable"
        logger.error(
            "call_integration_unavailable",
            extra={"call_id": str(call_id), "error_type": type(error).__name__},
        )
    except (ValueError, KeyError, TypeError, AttributeError, ApplicationError):
        status, reason = "failed", "invalid_stream"
    except Exception:
        status, reason = "failed", "pipeline_error"
        logger.error("call_pipeline_failed", extra={"call_id": str(call_id)})
    finally:
        for model in owned:
            with suppress(Exception):
                await model.close()
        if conversation_state is not None and conversation_store is not None:
            conversation_state.call_state.status = {
                "completed": CallStatus.COMPLETED,
                "failed": CallStatus.FAILED,
                "disconnected": CallStatus.DISCONNECTED,
            }[status]
            conversation_state.call_state.ended_at = datetime.now(UTC)
            try:
                async with asyncio.timeout(5):
                    await conversation_store.save(conversation_state, CONVERSATION_TTL_SECONDS)
            except Exception:
                logger.error("conversation_cleanup_failed", extra={"call_id": str(call_id)})
        if context is not None:
            post_call_input = None
            if conversation_state is not None:
                try:
                    post_call_input = PostCallInput.capture(conversation_state, call_summary)
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
            if status == "failed":
                # Someone rang and was not helped: the team should know and call back.
                with suppress(SQLAlchemyError, TimeoutError):
                    async with asyncio.timeout(5), service.sessions.begin() as session:
                        await set_tenant_scope(session, tenant_id)
                        await open_ticket(
                            session,
                            tenant_id,
                            source="call",
                            subject="A call could not be answered",
                            description="The receptionist could not take this call. "
                            "Call the person back if their number is shown.",
                            priority="high",
                            contact=caller_number,
                            call_id=call_id,
                            dedupe_key=f"call:{call_id}:failed",
                        )
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
