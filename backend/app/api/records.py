from typing import Annotated

from fastapi import APIRouter, Body, Query, status

from app.api.deps import CurrentUser, Session, WriterRole
from app.schemas.records import RecordCreate, RecordListQuery, RecordPage, RecordRead, RecordUpdate
from app.services import records

router = APIRouter(prefix="/records", tags=["records"])

BULK_MAX_ITEMS = 1000


@router.get("")
async def list_records(
    query: Annotated[RecordListQuery, Query()], _: CurrentUser, session: Session
) -> RecordPage:
    return await records.list_records(session, query)


@router.get("/{record_id}")
async def get_record(record_id: int, _: CurrentUser, session: Session) -> RecordRead:
    return RecordRead.model_validate(await records.get_record(session, record_id))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_record(data: RecordCreate, writer: WriterRole, session: Session) -> RecordRead:
    [record] = await records.create_records(session, [data], writer)
    await session.commit()
    return RecordRead.model_validate(record)


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
async def create_records_bulk(
    items: Annotated[list[RecordCreate], Body(min_length=1, max_length=BULK_MAX_ITEMS)],
    writer: WriterRole,
    session: Session,
) -> list[RecordRead]:
    created = await records.create_records(session, items, writer)
    await session.commit()
    return [RecordRead.model_validate(r) for r in created]


@router.patch("/{record_id}")
async def update_record(
    record_id: int, changes: RecordUpdate, writer: WriterRole, session: Session
) -> RecordRead:
    return RecordRead.model_validate(
        await records.update_record(session, record_id, changes, writer)
    )


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(record_id: int, writer: WriterRole, session: Session) -> None:
    await records.delete_record(session, record_id, writer)
