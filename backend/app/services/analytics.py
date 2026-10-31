import asyncio
import io
from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.worksheet._write_only import WriteOnlyWorksheet
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


def _cell(sheet: WriteOnlyWorksheet, value: Any) -> Any:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        # Always text: a title such as "=HYPERLINK(...)" must not become a live formula.
        cell = WriteOnlyCell(sheet, ILLEGAL_CHARACTERS_RE.sub("", value))
        cell.data_type = "s"
        return cell
    return value


def _append(sheet: WriteOnlyWorksheet, rows: Sequence[Sequence[Any]]) -> None:
    for row in rows:
        sheet.append([_cell(sheet, value) for value in row])


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
    buffer = io.BytesIO()
    try:
        # Building cells is CPU work (about 35 ms per 1,000 rows); threads keep the loop free.
        async for rows in result.partitions():
            await asyncio.to_thread(_append, sheet, rows)
        await asyncio.to_thread(workbook.save, buffer)
    finally:
        await result.close()
        if not sheet.closed:
            # The sheet spools to a temp file that only save() removes; Workbook.close() does
            # not touch it in write-only mode.
            sheet.close()
            sheet._writer.cleanup()
    return buffer.getvalue()
