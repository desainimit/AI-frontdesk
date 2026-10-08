import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.config.logging import request_id_var
from app.errors import ApplicationError as ApplicationError
from app.errors import AuthenticationError
from app.errors import ConflictError as ConflictError
from app.errors import NotFoundError as NotFoundError

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    body = ErrorResponse(error=ErrorDetail(code=code, message=message), request_id=request_id)
    return JSONResponse(
        status_code=status, content=body.model_dump(), headers={"X-Request-ID": request_id}
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        response = error_response(request, exc.status_code, exc.code, exc.public_message)
        if isinstance(exc, AuthenticationError):
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic errors can contain passwords, request bodies, and invalid URL parameters.
        return error_response(request, 422, "validation_error", "Request validation failed.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        response = error_response(request, exc.status_code, "http_error", "Request failed.")
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        token = request_id_var.set(getattr(request.state, "request_id", None))
        try:
            logger.error("unhandled_error", extra={"error_type": type(exc).__name__})
        finally:
            request_id_var.reset(token)
        return error_response(request, 500, "internal_error", "An unexpected error occurred.")
