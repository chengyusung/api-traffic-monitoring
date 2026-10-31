import logging
import uuid
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException

from app.core.logging import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)

# asyncmy raises OperationalError for every server errno it does not map, CHECK and unknown
# column included, so the class alone does not tell a lost connection from a bad statement.
TRANSIENT_ERRNOS = frozenset(
    {
        2002,  # can't connect through the socket
        2003,  # can't connect to the server
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection at the system level
        1040,  # too many connections
        1020,  # record changed since the snapshot (innodb_snapshot_isolation); like a deadlock
        1053,  # server shutdown in progress
        1158,  # error reading communication packets
        1159,  # timeout reading communication packets
        1160,  # error writing communication packets
        1161,  # timeout writing communication packets
        1203,  # max_user_connections reached
        1205,  # lock wait timeout
        1213,  # deadlock
        1290,  # server running with --read-only, as during a failover
        1317,  # query interrupted (KILL QUERY)
        1927,  # connection killed
        1969,  # max_statement_time exceeded
        4031,  # disconnected by the server for inactivity
    }
)


def driver_errno(exc: BaseException) -> int | None:
    """The MariaDB error number; None for anything else, so no message text reaches a log."""
    orig = getattr(exc, "orig", None)
    code = orig.args[0] if orig is not None and orig.args else None
    return code if isinstance(code, int) else None


def database_unavailable(exc: BaseException) -> bool:
    """A failure a later attempt can fix: the connection, a lock or a timeout, not the statement."""
    if isinstance(exc, sa_exc.TimeoutError):
        return True
    if not isinstance(exc, sa_exc.DBAPIError):
        return False
    return exc.connection_invalidated or driver_errno(exc) in TRANSIENT_ERRNOS


class AppError(Exception):
    status_code: ClassVar[int] = HTTPStatus.INTERNAL_SERVER_ERROR
    code: ClassVar[str] = "INTERNAL_SERVER_ERROR"

    def __init__(self, message: str, details: Sequence[Mapping[str, object]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


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


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: Sequence[Mapping[str, object]] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    error: dict[str, object] = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


async def _app_error(request: Request, exc: AppError) -> JSONResponse:
    # RFC 6750 §3: a 401 names the scheme the client should retry with.
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == HTTPStatus.UNAUTHORIZED else None
    return error_response(request, exc.status_code, exc.code, exc.message, headers, exc.details)


async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    status = HTTPStatus(exc.status_code)
    message = exc.detail if isinstance(exc.detail, str) else status.phrase
    return error_response(request, status, status.name, message, exc.headers)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Field paths and reasons only: echoing the rejected input could leak a password.
    message = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
    return error_response(request, HTTPStatus.UNPROCESSABLE_ENTITY, "VALIDATION_ERROR", message)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled exception", extra={"request_id": getattr(request.state, "request_id", None)}
    )
    return error_response(
        request, HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_SERVER_ERROR", "Internal server error."
    )


async def _database_error(request: Request, exc: Exception) -> JSONResponse:
    """503 only for what database_unavailable() accepts; a bad statement is a 500 bug."""
    if not database_unavailable(exc):
        return await _unhandled_error(request, exc)
    logger.warning(
        "database unavailable", extra={"error": type(exc).__name__, "errno": driver_errno(exc)}
    )
    return await _app_error(request, ServiceUnavailableError("Database unavailable."))


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(sa_exc.DBAPIError, _database_error)
    app.add_exception_handler(sa_exc.TimeoutError, _database_error)
    app.add_exception_handler(Exception, _unhandled_error)
