from collections.abc import Collection, Sequence

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiService, TrafficRecord
from app.schemas.records import RecordFilters, RecordListQuery


async def get(session: AsyncSession, record_id: int) -> TrafficRecord | None:
    return await session.get(TrafficRecord, record_id)


def filter_conditions(filters: RecordFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.service is not None:
        service_id = select(ApiService.id).where(ApiService.slug == filters.service)
        conditions.append(TrafficRecord.service_id == service_id.scalar_subquery())
    if filters.source is not None:
        conditions.append(TrafficRecord.source == filters.source)
    if filters.is_anomaly is not None:
        conditions.append(TrafficRecord.is_anomaly == filters.is_anomaly)
    if filters.start is not None:
        conditions.append(TrafficRecord.observed_at >= filters.start)
    if filters.end is not None:
        conditions.append(TrafficRecord.observed_at < filters.end)
    if filters.q:
        conditions.append(TrafficRecord.title.contains(filters.q, autoescape=True))
    return conditions


async def count(session: AsyncSession, filters: RecordFilters) -> int:
    query = select(func.count()).select_from(TrafficRecord).where(*filter_conditions(filters))
    return await session.scalar(query) or 0


async def list_page(
    session: AsyncSession, query: RecordListQuery
) -> tuple[Sequence[TrafficRecord], int]:
    conditions = filter_conditions(query)
    total = await count(session, query)
    column = getattr(TrafficRecord, query.sort.removeprefix("-"))
    descending = query.sort.startswith("-")
    # id breaks ties so equal sort values page deterministically.
    order_by = (
        (column.desc(), TrafficRecord.id.desc())
        if descending
        else (column.asc(), TrafficRecord.id.asc())
    )
    rows = await session.scalars(
        select(TrafficRecord)
        .where(*conditions)
        .order_by(*order_by)
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    return rows.all(), total


async def existing_event_ids(session: AsyncSession, event_ids: Collection[str]) -> set[str]:
    if not event_ids:
        return set()
    rows = await session.scalars(
        select(TrafficRecord.event_id).where(TrafficRecord.event_id.in_(event_ids))
    )
    return set(rows)


async def link_import_batch(
    session: AsyncSession, record_ids: Sequence[int], batch_id: int
) -> None:
    await session.execute(
        update(TrafficRecord)
        .where(TrafficRecord.id.in_(record_ids))
        .values(import_batch_id=batch_id)
        .execution_options(synchronize_session=False)
    )
