from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnprocessableError
from app.repositories import analytics
from app.schemas.analytics import Aggregate, ServiceAggregate, TrendPoint, TrendQuery
from app.schemas.records import RecordFilters

# A week of minutes; bounds the response the way EXPORT_MAX_ROWS bounds the export.
TREND_MAX_BUCKETS = 10_080


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
