"""SQL NULL and range semantics, so these run against the test MariaDB."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RecordSource, TrafficRecord
from app.schemas.analytics import TrendQuery
from app.schemas.records import RecordFilters
from app.services import analytics

T0 = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)


async def add_rows(
    session_factory: async_sessionmaker[AsyncSession], service_id: int, *at: datetime
) -> None:
    async with session_factory() as session:
        session.add_all(
            TrafficRecord(
                event_id=f"00000000-0000-4000-8000-{n:012d}",
                service_id=service_id,
                title="t",
                value=10,
                source=RecordSource.MANUAL,
                observed_at=observed_at,
                persisted_at=observed_at,
                threshold_applied=80,
                is_anomaly=False,
            )
            for n, observed_at in enumerate(at)
        )
        await session.commit()


async def test_an_empty_set_has_null_sum_and_avg_not_zero(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    async with session_factory() as session:
        summary = await analytics.summary(session, RecordFilters())
        categories = await analytics.categories(session, RecordFilters())
        trends = await analytics.trends(session, TrendQuery())

    assert summary.model_dump() == {
        "count": 0,
        "sum": None,
        "avg_per_observation": None,
        "min": None,
        "max": None,
        "anomaly_count": 0,
    }
    assert (categories, trends) == ([], [])


async def test_the_window_includes_start_and_excludes_end(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    start, end = T0, T0 + timedelta(minutes=1)
    just_before_end = end - timedelta(microseconds=1)
    await add_rows(
        session_factory,
        services["auth"],
        start - timedelta(microseconds=1),
        start,
        just_before_end,
        end,
    )
    window = {"start": start, "end": end}

    async with session_factory() as session:
        summary = await analytics.summary(session, RecordFilters(**window))
        trends = await analytics.trends(session, TrendQuery(**window))

    assert summary.count == 2
    assert [(p.bucket_start, p.count) for p in trends] == [(T0, 2)]
