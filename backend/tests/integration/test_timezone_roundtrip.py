import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, type_coerce
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiService, RecordSource, TrafficRecord

TAIPEI = timezone(timedelta(hours=8))


async def add_record(session: AsyncSession, observed_at: datetime) -> None:
    service = ApiService(slug="payment", display_name="Payment API", alert_threshold=80)
    session.add(service)
    await session.flush()
    session.add(
        TrafficRecord(
            event_id=str(uuid.uuid4()),
            service_id=service.id,
            title="tz round-trip",
            value=81,
            source=RecordSource.MANUAL,
            observed_at=observed_at,
            persisted_at=datetime.now(UTC),
            threshold_applied=80,
            is_anomaly=True,
        )
    )
    await session.flush()


async def test_aware_datetime_is_stored_as_naive_utc_and_read_back_aware(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    written = datetime(2026, 10, 31, 16, 0, 4, tzinfo=TAIPEI)
    async with session_factory() as session:
        await add_record(session, written)
        await session.commit()

    async with session_factory() as session:
        read_back, stored = (
            await session.execute(
                select(
                    TrafficRecord.observed_at,
                    type_coerce(TrafficRecord.observed_at, DATETIME(fsp=6)).label("stored"),
                )
            )
        ).one()

    assert stored == datetime(2026, 10, 31, 8, 0, 4)
    assert read_back == written
    assert read_back.utcoffset() == timedelta(0)


async def test_naive_datetime_raises_at_the_type_layer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(StatementError) as excinfo:
            await add_record(session, datetime(2026, 10, 31, 16, 0, 4))

    assert isinstance(excinfo.value.orig, ValueError)
