import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.buffer import IngestionBuffer, IngestionEvent
from app.ingestion.writer import BatchWriter, to_record
from app.models import RecordSource, SystemLog, TrafficRecord


def event(service_id: int, n: int) -> IngestionEvent:
    return IngestionEvent(
        event_id=str(uuid.uuid4()),
        service_id=service_id,
        service_slug="payment",
        title="Payment API Traffic",
        value=79 + n,
        source=RecordSource.SIMULATOR,
        observed_at=datetime(2026, 10, 31, 11, 30, n, tzinfo=UTC),
        threshold_applied=80,
        is_anomaly=79 + n > 80,
    )


async def accepted(events: list[IngestionEvent]) -> IngestionBuffer:
    buffer = IngestionBuffer(capacity=10)
    for item in events:
        await buffer.accept(item)
    return buffer


async def flush_and_stop(buffer: IngestionBuffer, sessions: async_sessionmaker[AsyncSession]):
    """close() first, so run() writes what is queued without waiting for the interval."""
    writer = BatchWriter(
        buffer,
        sessions,
        batch_max_size=100,
        flush_interval_s=5,
        retry_initial_s=1,
        retry_max_s=30,
    )
    writer.close()
    await writer.run()
    return writer


async def stored(sessions: async_sessionmaker[AsyncSession]) -> list[TrafficRecord]:
    async with sessions() as session:
        return list(await session.scalars(select(TrafficRecord).order_by(TrafficRecord.id)))


async def test_a_batch_is_inserted_in_one_commit_and_released(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    events = [event(services["payment"], n) for n in range(3)]
    buffer = await accepted(events)

    writer = await flush_and_stop(buffer, session_factory)

    rows = await stored(session_factory)
    assert [row.event_id for row in rows] == [item.event_id for item in events]
    assert [row.is_anomaly for row in rows] == [False, False, True]
    assert all(row.source is RecordSource.SIMULATOR and row.created_by is None for row in rows)
    assert len({row.persisted_at for row in rows}) == 1, "one batch, one persisted_at"
    assert buffer.total_uncommitted == 0
    assert writer.last_commit_at is not None


async def test_after_a_duplicate_only_the_missing_rows_are_inserted(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    events = [event(services["payment"], n) for n in range(3)]
    landed_at = datetime(2026, 10, 31, 11, 30, 5, tzinfo=UTC)
    async with session_factory() as session:
        # A commit that reached MariaDB but whose ack was lost before the release.
        session.add(to_record(events[1], landed_at))
        await session.commit()
    buffer = await accepted(events)

    writer = await flush_and_stop(buffer, session_factory)

    rows = await stored(session_factory)
    assert sorted(row.event_id for row in rows) == sorted(item.event_id for item in events)
    async with session_factory() as session:
        counts = await session.execute(
            select(func.count(), func.count(TrafficRecord.event_id.distinct()))
        )
    assert tuple(counts.one()) == (3, 3)
    assert next(row for row in rows if row.event_id == events[1].event_id).persisted_at == landed_at
    assert buffer.total_uncommitted == 0, "the whole batch, duplicate included, is released"
    assert writer.retry_count == 0


async def test_a_foreign_key_violation_stops_the_writer_keeps_the_batch_and_is_audited(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    unknown_service = max(services.values()) + 1000
    buffer = await accepted([event(unknown_service, 1)])

    writer = await flush_and_stop(buffer, session_factory)

    assert await stored(session_factory) == []
    assert buffer.total_uncommitted == 1, "a permanent failure must not release the batch"
    assert writer.retry_count == 0
    async with session_factory() as session:
        logs = list(await session.scalars(select(SystemLog)))
    assert [(log.level, log.event_type) for log in logs] == [("ERROR", "writer.failed")]
    assert logs[0].context == {"error": "IntegrityError", "batch_size": 1}
