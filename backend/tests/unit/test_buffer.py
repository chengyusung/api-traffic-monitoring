import asyncio
import random
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import asyncmy.errors
import pytest
from sqlalchemy.exc import DataError, OperationalError

from app.ingestion.buffer import IngestionBuffer, IngestionEvent, WriterState
from app.ingestion.writer import BatchWriter
from app.models import RecordSource, SystemLog, TrafficRecord

FLUSH_S = 5.0


class FakeClock:
    """Virtual monotonic time that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class CeilingJitter(random.Random):
    """Full jitter that always draws the ceiling, so the backoff cap itself is observable."""

    def uniform(self, a: float, b: float) -> float:
        return b


class FakeSessions:
    """Stands in for async_sessionmaker. Commits raise `failures` in order, then succeed.

    With hold=True every commit waits for one `permits.release()` from the test.
    """

    def __init__(self, failures: Iterable[Exception] = (), *, hold: bool = False) -> None:
        self.failures = list(failures)
        self.hold = hold
        self.permits = asyncio.Semaphore(0)
        self.opened = 0
        self.attempts: list[list[TrafficRecord]] = []
        self.committed: list[list[TrafficRecord]] = []
        self.audit: list[SystemLog] = []
        self._changed = asyncio.Condition()

    def __call__(self) -> "FakeSession":
        self.opened += 1
        return FakeSession(self)

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with self._changed:
            await self._changed.wait_for(predicate)

    async def notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()


class FakeSession:
    def __init__(self, sessions: FakeSessions) -> None:
        self.sessions = sessions
        self.rows: list[TrafficRecord] = []
        self.logs: list[SystemLog] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        try:
            yield
        except BaseException:
            await self.rollback()
            raise
        await self.commit()

    def add_all(self, rows: Iterable[TrafficRecord]) -> None:
        self.rows.extend(rows)

    def add(self, row: SystemLog) -> None:
        self.logs.append(row)

    async def rollback(self) -> None:
        self.rows.clear()
        self.logs.clear()

    async def commit(self) -> None:
        sessions = self.sessions
        if self.logs:
            sessions.audit.extend(self.logs)
            await sessions.notify()
            return
        sessions.attempts.append(list(self.rows))
        await sessions.notify()
        if sessions.hold:
            await sessions.permits.acquire()
        if sessions.failures:
            raise sessions.failures.pop(0)
        sessions.committed.append(list(self.rows))
        await sessions.notify()


def event(n: int) -> IngestionEvent:
    return IngestionEvent(
        event_id=str(uuid.uuid4()),
        service_id=3,
        service_slug="payment",
        title="Payment API Traffic",
        value=70 + n,
        source=RecordSource.SIMULATOR,
        observed_at=datetime(2026, 10, 31, 11, 30, n % 60, tzinfo=UTC),
        threshold_applied=80,
        is_anomaly=70 + n > 80,
    )


def payload(row: TrafficRecord) -> tuple[object, ...]:
    return (
        row.event_id,
        row.service_id,
        row.title,
        row.value,
        row.source,
        row.observed_at,
        row.threshold_applied,
        row.is_anomaly,
    )


def db_down() -> OperationalError:
    lost = asyncmy.errors.OperationalError(2013, "Lost connection to MySQL server during query")
    return OperationalError("INSERT INTO traffic_records", {}, lost)


def make_writer(
    buffer: IngestionBuffer, sessions: FakeSessions, clock: FakeClock, batch_max_size: int = 100
) -> BatchWriter:
    return BatchWriter(
        buffer,
        sessions,  # type: ignore[arg-type]
        batch_max_size=batch_max_size,
        flush_interval_s=FLUSH_S,
        retry_initial_s=1,
        retry_max_s=30,
        clock=clock,
        sleep=clock.sleep,
        rng=CeilingJitter(),
    )


@asynccontextmanager
async def running(writer: BatchWriter) -> AsyncIterator[None]:
    task = asyncio.create_task(writer.run())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def let_tasks_run() -> None:
    """Yield to the event loop until other ready tasks are blocked again (no wall-clock wait)."""
    for _ in range(50):
        await asyncio.sleep(0)


async def test_a_full_batch_flushes_without_waiting_for_the_interval() -> None:
    clock, sessions = FakeClock(), FakeSessions()
    buffer = IngestionBuffer(capacity=1000, clock=clock)
    for n in range(101):
        await buffer.accept(event(n))

    async with running(make_writer(buffer, sessions, clock)):
        await sessions.wait_for(lambda: len(sessions.committed) == 1)
        assert len(sessions.committed[0]) == 100
        assert clock.now == 0, "the 100-item batch waited for the timer"

        await sessions.wait_for(lambda: len(sessions.committed) == 2)
        assert len(sessions.committed[1]) == 1


async def test_a_partial_batch_flushes_once_the_interval_passes() -> None:
    clock, sessions = FakeClock(), FakeSessions()
    buffer = IngestionBuffer(capacity=1000, clock=clock)
    await buffer.accept(event(1))

    async with running(make_writer(buffer, sessions, clock)):
        await sessions.wait_for(lambda: len(sessions.committed) == 1)

    assert len(sessions.committed[0]) == 1
    assert clock.sleeps == [FLUSH_S]
    assert buffer.total_uncommitted == 0


async def test_an_empty_queue_never_opens_a_transaction() -> None:
    clock, sessions = FakeClock(), FakeSessions()
    buffer = IngestionBuffer(capacity=1000, clock=clock)

    async with running(make_writer(buffer, sessions, clock)):
        await let_tasks_run()
        assert sessions.opened == 0
        assert clock.sleeps == [], "an idle writer should block on the queue, not poll a timer"


async def test_capacity_counts_the_batch_the_writer_holds() -> None:
    clock, sessions = FakeClock(), FakeSessions(hold=True)
    buffer = IngestionBuffer(capacity=2, clock=clock)
    first, second = event(1), event(2)
    await buffer.accept(first)
    await buffer.accept(second)
    writer = make_writer(buffer, sessions, clock, batch_max_size=2)

    async with running(writer):
        await sessions.wait_for(lambda: len(sessions.attempts) == 1)
        stats = writer.stats()
        assert (stats.queued, stats.in_flight, stats.total_uncommitted) == (0, 2, 2)
        assert stats.writer_state is WriterState.IN_FLIGHT

        third = asyncio.create_task(buffer.accept(event(3)))
        await let_tasks_run()
        assert not third.done(), "accept() went through while the writer held the full capacity"

        sessions.permits.release()
        await third

    assert [row.event_id for row in sessions.committed[0]] == [first.event_id, second.event_id]


async def test_capacity_is_released_only_after_a_confirmed_commit_and_retries_reuse_events() -> (
    None
):
    clock, sessions = FakeClock(), FakeSessions([db_down()], hold=True)
    buffer = IngestionBuffer(capacity=10, clock=clock)
    for n in range(2):
        await buffer.accept(event(n))
    writer = make_writer(buffer, sessions, clock, batch_max_size=2)

    async with running(writer):
        await sessions.wait_for(lambda: len(sessions.attempts) == 1)
        assert buffer.total_uncommitted == 2, "taking the batch off the queue released capacity"

        sessions.permits.release()
        await sessions.wait_for(lambda: len(sessions.attempts) == 2)
        stats = writer.stats()
        assert stats.total_uncommitted == 2, "a failed commit released capacity"
        assert stats.retry_count == 1
        assert stats.last_commit_at is None

        sessions.permits.release()
        await sessions.wait_for(lambda: len(sessions.committed) == 1)
        stats = writer.stats()
        assert stats.total_uncommitted == 0
        assert stats.last_commit_at is not None
        assert stats.writer_state is WriterState.IDLE

    first_try, retry = sessions.attempts
    assert [payload(row) for row in retry] == [payload(row) for row in first_try]


async def test_backoff_doubles_from_one_second_and_caps_at_thirty() -> None:
    clock, sessions = FakeClock(), FakeSessions([db_down() for _ in range(8)])
    buffer = IngestionBuffer(capacity=10, clock=clock)
    await buffer.accept(event(1))

    async with running(make_writer(buffer, sessions, clock, batch_max_size=1)):
        await sessions.wait_for(lambda: len(sessions.committed) == 1)

    assert clock.sleeps == [1, 2, 4, 8, 16, 30, 30, 30]


async def test_a_permanent_error_stops_the_writer_and_keeps_the_batch() -> None:
    clock = FakeClock()
    sessions = FakeSessions([DataError("INSERT INTO traffic_records", {}, ValueError("bad"))])
    buffer = IngestionBuffer(capacity=10, clock=clock)
    for n in range(2):
        await buffer.accept(event(n))
    writer = make_writer(buffer, sessions, clock, batch_max_size=1)

    async with running(writer):
        await sessions.wait_for(lambda: len(sessions.audit) == 1)
        await let_tasks_run()
        stats = writer.stats()
        assert stats.writer_state is WriterState.UNAVAILABLE
        assert (stats.queued, stats.retry_pending, stats.total_uncommitted) == (1, 1, 2)
        assert len(sessions.attempts) == 1, "the writer kept taking batches after a permanent error"

    [log] = sessions.audit
    assert (log.level, log.event_type) == ("ERROR", "writer.failed")


async def test_a_cancelled_accept_hands_a_granted_reservation_back() -> None:
    buffer = IngestionBuffer(capacity=1, clock=FakeClock())
    first = event(1)
    await buffer.accept(first)
    waiting = asyncio.create_task(buffer.accept(event(2)))
    await let_tasks_run()
    assert not waiting.done()

    assert buffer.take_nowait() == first
    buffer.release(1)  # the freed slot is granted to the waiting accept() ...
    waiting.cancel()  # ... which is cancelled before it can enqueue
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert buffer.total_uncommitted == 0
    third = event(3)
    accepted = asyncio.create_task(buffer.accept(third))
    await let_tasks_run()
    assert accepted.done(), "the cancelled accept() kept its reservation"
    assert buffer.take_nowait() == third


@pytest.mark.parametrize("steps_before_cancel", [0, 1, 2])
async def test_cancelling_accept_at_any_await_point_never_leaks_capacity(
    steps_before_cancel: int,
) -> None:
    buffer = IngestionBuffer(capacity=1, clock=FakeClock())
    accepting = asyncio.create_task(buffer.accept(event(1)))
    for _ in range(steps_before_cancel):
        await asyncio.sleep(0)
    accepting.cancel()
    with suppress(asyncio.CancelledError):
        await accepting
    if buffer.take_nowait() is not None:  # accepted before the cancel landed
        buffer.release(1)

    assert buffer.total_uncommitted == 0
    later = asyncio.create_task(buffer.accept(event(2)))
    await let_tasks_run()
    assert later.done(), "a cancelled accept() kept its reservation"
