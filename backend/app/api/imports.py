from http import HTTPStatus

from fastapi import APIRouter, Request, UploadFile, status
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.deps import Session, WriterRole
from app.core.config import settings
from app.core.errors import error_response
from app.schemas.imports import ImportBatchRead
from app.services import imports

router = APIRouter(prefix="/imports", tags=["imports"])
# Boundaries and part headers that wrap the file in a multipart body.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class UploadLimitMiddleware:
    """413 for an import whose Content-Length is over the limit, before Starlette spools it.

    A chunked upload declares no length, so it is still read in full; a proxy limit covers it.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == self.path:
            length = Headers(scope=scope).get("content-length", "")
            limit = settings.import_max_bytes + MULTIPART_OVERHEAD_BYTES
            if length.isdigit() and int(length) > limit:
                response = error_response(
                    Request(scope),
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "PAYLOAD_TOO_LARGE",
                    f"The upload is larger than {limit} bytes.",
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_import(file: UploadFile, writer: WriterRole, session: Session) -> ImportBatchRead:
    """Import a CSV or JSON file, all or nothing.

    CSV header: `event_id,service_slug,title,value,observed_at`; JSON: an array of objects with
    the same fields. A rejected file returns 422 (or 409 when an event_id already exists) whose
    `error.details` lists `{row, field, message}`; the batch is recorded either way. A body
    whose Content-Length is over IMPORT_MAX_BYTES plus 64 KiB is a 413 before it is read.
    """
    # One byte past the limit is enough to reject the file.
    content = await file.read(settings.import_max_bytes + 1)
    batch = await imports.import_file(session, file.filename or "", content, writer)
    return ImportBatchRead.model_validate(batch)


@router.get("/{batch_id}")
async def get_import(batch_id: int, user: WriterRole, session: Session) -> ImportBatchRead:
    return ImportBatchRead.model_validate(await imports.get_batch(session, batch_id, user))
