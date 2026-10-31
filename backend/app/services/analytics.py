import asyncio
import io
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import Enum
from typing import Any

from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import UnprocessableError
from app.repositories import analytics, records
from app.schemas.analytics import Aggregate, ServiceAggregate, TrendPoint, TrendQuery
from app.schemas.records import RecordFilters

# A week of minutes; bounds the response the way EXPORT_MAX_ROWS bounds the export.
TREND_MAX_BUCKETS = 10_080
# Order matches repositories.analytics.stream_export; Excel stores no time zone, hence _utc.
EXPORT_HEADER = (
    "id",
    "event_id",
    "service",
    "title",
    "value",
    "source",
    "created_by",
    "observed_at_utc",
    "persisted_at_utc",
    "threshold_applied",
    "is_anomaly",
    "import_batch_id",
)


async def summary(session: AsyncSession, filters: RecordFilters) -> Aggregate:
    return Aggregate.model_validate(dict(await analytics.summary(session, filters)))


async def categories(session: AsyncSession, filters: RecordFilters) -> list[ServiceAggregate]:
    rows = await analytics.by_service(session, filters)
    return [ServiceAggregate.model_validate(dict(row)) for row in rows]


async def trends(session: AsyncSession, query: TrendQuery) -> list[TrendPoint]:
    """One point per bucket that has rows; an empty bucket is absent, not zero."""
    rows = await analytics.trend(session, query, query.bucket, TREND_MAX_BUCKETS + 1)
    if len(rows) > TREND_MAX_BUCKETS:
        raise UnprocessableError(
            f"More than {TREND_MAX_BUCKETS} {query.bucket} buckets; "
            "narrow start / end or use bucket=hour."
        )
    return [TrendPoint.model_validate(dict(row)) for row in rows]


def _cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, Enum):
        return value.value
    return value


def _append(append: Callable[[list[Any]], None], rows: Sequence[Sequence[Any]]) -> None:
    for row in rows:
        append([_cell(value) for value in row])


async def export_xlsx(session: AsyncSession, filters: RecordFilters) -> bytes:
    """The filtered detail rows as .xlsx, or 422 when more than EXPORT_MAX_ROWS match.

    The count and the rows come from one transaction, so they see the same snapshot.
    """
    total = await records.count(session, filters)
    if total > settings.export_max_rows:
        raise UnprocessableError(
            f"{total} rows match; the export limit is {settings.export_max_rows}. "
            "Narrow the filters."
        )
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("records")
    sheet.append(EXPORT_HEADER)
    result = await analytics.stream_export(session, filters, settings.export_max_rows)
    # Building cells is CPU work (about 35 ms per 1,000 rows); threads keep the event loop free.
    async for rows in result.partitions():
        await asyncio.to_thread(_append, sheet.append, rows)
    buffer = io.BytesIO()
    await asyncio.to_thread(workbook.save, buffer)
    return buffer.getvalue()
