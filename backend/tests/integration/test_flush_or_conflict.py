import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.db import flush_or_conflict
from app.models import RecordSource, TrafficRecord


def record(service_id: int, event_id: str) -> TrafficRecord:
    now = datetime.now(UTC)
    return TrafficRecord(
        event_id=event_id,
        service_id=service_id,
        title="t",
        value=1,
        source=RecordSource.MANUAL,
        observed_at=now,
        persisted_at=now,
        threshold_applied=1,
        is_anomaly=False,
    )


async def test_a_duplicate_key_becomes_a_conflict(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    event_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(record(services["auth"], event_id))
        await session.commit()
        session.add(record(services["auth"], event_id))

        with pytest.raises(ConflictError):
            await flush_or_conflict(session, "duplicate")


async def test_a_foreign_key_violation_is_not_a_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(record(999_999, str(uuid.uuid4())))

        with pytest.raises(IntegrityError):
            await flush_or_conflict(session, "duplicate")
