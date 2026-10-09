import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.api.dependencies import get_resources
from app.core.resources import Dependency, Resources

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)


class LivenessResponse(BaseModel):
    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "unavailable"]]


@router.get("/live")
async def live(response: Response) -> LivenessResponse:
    response.headers["Cache-Control"] = "no-store"
    return LivenessResponse()


async def check_dependency(
    name: str, dependency: Dependency, timeout_seconds: float
) -> Literal["ok", "unavailable"]:
    try:
        async with asyncio.timeout(timeout_seconds):
            await dependency.ping()
    except Exception as exc:
        # Deliberately contain dependency errors at the readiness boundary.
        logger.warning(
            "dependency_unavailable", extra={"dependency": name, "error_type": type(exc).__name__}
        )
        return "unavailable"
    return "ok"


@router.get("/ready", responses={503: {"model": ReadinessResponse}})
async def ready(
    request: Request,
    response: Response,
    resources: Annotated[Resources, Depends(get_resources)],
) -> ReadinessResponse:
    timeout: float = request.app.state.settings.dependency_timeout_seconds
    database, redis = await asyncio.gather(
        check_dependency("postgresql", resources.database, timeout),
        check_dependency("redis", resources.redis, timeout),
    )
    checks: dict[str, Literal["ok", "unavailable"]] = {"postgresql": database, "redis": redis}
    healthy = all(value == "ok" for value in checks.values())
    response.status_code = 200 if healthy else 503
    response.headers["Cache-Control"] = "no-store"
    return ReadinessResponse(status="ready" if healthy else "not_ready", checks=checks)
