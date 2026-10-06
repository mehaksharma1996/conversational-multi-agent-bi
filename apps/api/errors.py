"""Safe, correlation-aware API error handling."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.models import ErrorBody, ErrorDetail, ErrorResponse
from packages.observability import bind_request_id, error_category

LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[ErrorDetail] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers or {}


class ResourceNotFoundError(ApiError):
    def __init__(self, resource_name: str) -> None:
        super().__init__(404, "resource_not_found", f"{resource_name} was not found.")


class ResourceConflictError(ApiError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class AuthenticationError(ApiError):
    def __init__(self, message: str = "Valid authentication credentials are required.") -> None:
        super().__init__(
            401,
            "authentication_required",
            message,
            headers={"WWW-Authenticate": "Bearer"},
        )


class AuthorizationError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            403,
            "permission_denied",
            "You do not have permission to perform this operation.",
        )


class AuthenticationUnavailableError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "authentication_unavailable",
            "The authentication service is temporarily unavailable.",
        )


def request_id_for(request: Request) -> str:
    return str(getattr(request.state, "request_id", uuid4().hex))


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = request_id_for(request)
    request.state.error_code = code
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=request_id,
            details=details or [],
        )
    )
    response_headers = {"X-Request-ID": request_id}
    response_headers.update(headers or {})
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=response_headers,
    )


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        details = [
            ErrorDetail(
                field=".".join(str(part) for part in error.get("loc", ())),
                message=str(error.get("msg", "Invalid value.")),
                type=str(error.get("type", "validation_error")),
            )
            for error in exc.errors()
        ]
        return error_response(
            request,
            status_code=422,
            code="invalid_request",
            message="The request did not pass validation.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        message = "Resource not found." if exc.status_code == 404 else "Request failed."
        return error_response(
            request,
            status_code=exc.status_code,
            code="http_error",
            message=message,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_for(request)
        LOGGER.exception("api_request_failed request_id=%s", request_id, exc_info=exc)
        observability = getattr(request.app.state, "observability", None)
        if observability is not None:
            with bind_request_id(request_id):
                observability.telemetry.emit(
                    "api.unhandled_error",
                    outcome="failure",
                    error_category=error_category(exc),
                )
        return error_response(
            request,
            status_code=500,
            code="internal_error",
            message="The request encountered an unexpected internal error.",
        )


STANDARD_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Authentication required"},
    403: {"model": ErrorResponse, "description": "Permission denied"},
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Resource state conflict"},
    413: {"model": ErrorResponse, "description": "Upload limit exceeded"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    500: {"model": ErrorResponse, "description": "Sanitized internal error"},
    503: {"model": ErrorResponse, "description": "Dependency unavailable"},
}
