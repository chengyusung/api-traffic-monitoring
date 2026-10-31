import logging
import uuid
from http import HTTPStatus
from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException

from app.core.logging import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)

# Connection-level failures only: IntegrityError and DataError stay bugs or conflicts.
DATABASE_UNAVAILABLE = (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)


class AppError(Exception):
    status_code: ClassVar[int] = HTTPStatus.INTERNAL_SERVER_ERROR
    code: ClassVar[str] = "INTERNAL_SERVER_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnauthorizedError(AppError):
    status_code = HTTPStatus.UNAUTHORIZED
    code = "UNAUTHORIZED"


class TokenExpiredError(UnauthorizedError):
    code = "TOKEN_EXPIRED"


class ForbiddenError(AppError):
    status_code = HTTPStatus.FORBIDDEN
    code = "FORBIDDEN"


class NotFoundError(AppError):
    status_code = HTTPStatus.NOT_FOUND
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = HTTPStatus.CONFLICT
    code = "CONFLICT"


class UnprocessableError(AppError):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "VALIDATION_ERROR"


class ServiceUnavailableError(AppError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    code = "SERVICE_UNAVAILABLE"


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


async def _app_error(request: Request, exc: AppError) -> JSONResponse:
    # RFC 6750 §3: a 401 names the scheme the client should retry with.
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == HTTPStatus.UNAUTHORIZED else None
    return _error_response(request, exc.status_code, exc.code, exc.message, headers)


async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    status = HTTPStatus(exc.status_code)
    message = exc.detail if isinstance(exc.detail, str) else status.phrase
    return _error_response(request, status, status.name, message, exc.headers)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Field paths and reasons only: echoing the rejected input could leak a password.
    message = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
    return _error_response(request, HTTPStatus.UNPROCESSABLE_ENTITY, "VALIDATION_ERROR", message)


async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("database unavailable", extra={"error": type(exc).__name__})
    return await _app_error(request, ServiceUnavailableError("Database unavailable."))


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled exception", extra={"request_id": getattr(request.state, "request_id", None)}
    )
    return _error_response(
        request, HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_SERVER_ERROR", "Internal server error."
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    for unavailable in DATABASE_UNAVAILABLE:
        app.add_exception_handler(unavailable, _database_unavailable)
    app.add_exception_handler(Exception, _unhandled_error)
