from fastapi import APIRouter, UploadFile, status

from app.api.deps import Session, WriterRole
from app.core.config import settings
from app.schemas.imports import ImportBatchRead
from app.services import imports

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_import(file: UploadFile, writer: WriterRole, session: Session) -> ImportBatchRead:
    """Import a CSV or JSON file, all or nothing.

    CSV header: `event_id,service_slug,title,value,observed_at`; JSON: an array of objects with
    the same fields. A rejected file returns 422 (or 409 when an event_id already exists) whose
    `error.details` lists `{row, field, message}`; the batch is recorded either way.
    """
    # One byte past the limit is enough to reject the file.
    content = await file.read(settings.import_max_bytes + 1)
    batch = await imports.import_file(session, file.filename or "", content, writer)
    return ImportBatchRead.model_validate(batch)


@router.get("/{batch_id}")
async def get_import(batch_id: int, user: WriterRole, session: Session) -> ImportBatchRead:
    return ImportBatchRead.model_validate(await imports.get_batch(session, batch_id, user))
