import logging
from time import perf_counter
from typing import cast
from uuid import UUID, uuid4

from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.context import ActorType, RequestContext
from app.core.logging import request_id_var
from app.core.telemetry import Telemetry

logger = logging.getLogger(__name__)


class RequestTelemetryMiddleware:
    """Record allow-listed metadata only, excluding raw URLs, query strings, bodies and headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("x-request-id", "")
        try:
            request_id = str(UUID(supplied))
        except ValueError:
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = perf_counter()
        status = 500
        telemetry = cast(Telemetry, scope["app"].state.telemetry)
        method = str(scope.get("method", "OTHER"))
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            method = "OTHER"

        async def send_response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
                # Sign-in and platform answers are never cached, errors included.
                if scope.get("path", "").startswith(("/api/v1/platform", "/api/v1/auth")):
                    MutableHeaders(scope=message)["Cache-Control"] = "no-store"
                context = scope["state"].get("context")
                if (
                    isinstance(context, RequestContext)
                    and context.actor_type == ActorType.PLATFORM_SUPPORT
                ):
                    MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        try:
            with telemetry.tracer.start_as_current_span(
                "http.request",
                context=TraceContextTextMapPropagator().extract(dict(Headers(scope=scope))),
                kind=SpanKind.SERVER,
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    await self.app(scope, receive, send_response)
                finally:
                    # The router has resolved a template; never export the caller-controlled URL.
                    route = getattr(scope.get("route"), "path", "unmatched")
                    attributes = {
                        "http.request.method": method,
                        "http.route": str(route),
                        "http.response.status_code": status,
                    }
                    span.set_attributes(attributes)
                    span.update_name(f"{method} {route}")
                    if status >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                    elapsed = (perf_counter() - started) * 1000
                    telemetry.request_duration.record(elapsed / 1000, attributes)
                    context = scope["state"].get("context")
                    correlation: dict[str, str] = {}
                    if isinstance(context, RequestContext):
                        correlation["tenant_id"] = str(context.tenant_id)
                        if context.platform_user_id is not None:
                            correlation["platform_user_id"] = str(context.platform_user_id)
                        if context.support_session_id is not None:
                            correlation["support_session_id"] = str(context.support_session_id)
                        if context.call_id is not None:
                            correlation["call_id"] = str(context.call_id)
                    logger.info(
                        "request_completed",
                        extra={
                            "method": method,
                            "status_code": status,
                            "duration_ms": round(elapsed, 2),
                            **correlation,
                        },
                    )
        finally:
            request_id_var.reset(token)
