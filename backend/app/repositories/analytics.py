from collections.abc import Sequence
from typing import Any, Literal

from sqlalchemy import Label, RowMapping, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncResult, AsyncSession

from app.models import ApiService, TrafficRecord
from app.models.types import UTCDateTime
from app.repositories.records import filter_conditions
from app.schemas.records import RecordFilters

# DATE_FORMAT reads the stored UTC wall clock, so buckets are UTC whatever the session zone.
BUCKET_FORMATS = {"minute": "%Y-%m-%d %H:%i:00", "hour": "%Y-%m-%d %H:00:00"}


def _metrics() -> tuple[Label[Any], ...]:
    value = TrafficRecord.value
    return (
        func.count().label("count"),
        func.sum(value).label("sum"),
        func.avg(value).label("avg_per_observation"),
        func.min(value).label("min"),
        func.max(value).label("max"),
        func.count(case((TrafficRecord.is_anomaly, 1))).label("anomaly_count"),
    )


async def summary(session: AsyncSession, filters: RecordFilters) -> RowMapping:
    result = await session.execute(select(*_metrics()).where(*filter_conditions(filters)))
    return result.mappings().one()


async def by_service(session: AsyncSession, filters: RecordFilters) -> Sequence[RowMapping]:
    result = await session.execute(
        select(ApiService.slug.label("service"), *_metrics())
        .select_from(TrafficRecord)
        .join(ApiService, TrafficRecord.service_id == ApiService.id)
        .where(*filter_conditions(filters))
        .group_by(ApiService.slug)
        .order_by(ApiService.slug)
    )
    return result.mappings().all()


async def trend(
    session: AsyncSession,
    filters: RecordFilters,
    bucket: Literal["minute", "hour"],
    limit: int,
) -> Sequence[RowMapping]:
    bucket_start = cast(
        func.date_format(TrafficRecord.observed_at, BUCKET_FORMATS[bucket]), UTCDateTime()
    ).label("bucket_start")
    result = await session.execute(
        select(bucket_start, *_metrics())
        .where(*filter_conditions(filters))
        .group_by(bucket_start)
        .order_by(bucket_start)
        .limit(limit)
    )
    return result.mappings().all()


async def stream_export(
    session: AsyncSession, filters: RecordFilters, limit: int
) -> AsyncResult[tuple[Any, ...]]:
    """Detail rows in the records list's default order, fetched from the server in chunks."""
    return await session.stream(
        select(
            TrafficRecord.id,
            TrafficRecord.event_id,
            ApiService.slug,
            TrafficRecord.title,
            TrafficRecord.value,
            TrafficRecord.source,
            TrafficRecord.created_by,
            TrafficRecord.observed_at,
            TrafficRecord.persisted_at,
            TrafficRecord.threshold_applied,
            TrafficRecord.is_anomaly,
            TrafficRecord.import_batch_id,
        )
        .join(ApiService, TrafficRecord.service_id == ApiService.id)
        .where(*filter_conditions(filters))
        .order_by(TrafficRecord.observed_at.desc(), TrafficRecord.id.desc())
        .limit(limit)
        .execution_options(yield_per=1000)
    )
