from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiService, TrafficRecord
from app.schemas.records import RecordFilters, RecordListQuery

# traffic_records has no created_at column (architecture.md §5.1); persisted_at is when the
# system created the row, so the `created_at` sort key orders by it.
SORT_COLUMNS = {
    "observed_at": TrafficRecord.observed_at,
    "value": TrafficRecord.value,
    "created_at": TrafficRecord.persisted_at,
}


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


async def list_page(
    session: AsyncSession, query: RecordListQuery
) -> tuple[Sequence[TrafficRecord], int]:
    conditions = filter_conditions(query)
    total = await session.scalar(select(func.count()).select_from(TrafficRecord).where(*conditions))
    column = SORT_COLUMNS[query.sort.removeprefix("-")]
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
    return rows.all(), total or 0
