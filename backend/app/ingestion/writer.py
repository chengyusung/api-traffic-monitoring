"""Batch writer (ADR-001): one transaction per batch, capacity released only after commit."""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import is_duplicate_key
from app.ingestion.buffer import IngestionBuffer, IngestionEvent, IngestionStats, WriterState
from app.models import TrafficRecord, utcnow
from app.services import audit

logger = logging.getLogger(__name__)


# asyncmy raises OperationalError for every server errno it does not map, CHECK and unknown
# column included, so the class alone does not tell a lost connection from a bad batch.
TRANSIENT_ERRNOS = frozenset(
    {
        2002,  # can't connect through the socket
        2003,  # can't connect to the server
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection at the system level
        1040,  # too many connections
        1053,  # server shutdown in progress
        1205,  # lock wait timeout
        1213,  # deadlock
        1927,  # connection killed
        4031,  # disconnected by the server for inactivity
    }
)


def is_transient(exc: Exception) -> bool:
    """What a later attempt can fix is retried forever; anything else stops the writer."""
    if isinstance(exc, (TimeoutError, PoolTimeoutError)):
        return True
    if not isinstance(exc, DBAPIError):
        return False
    if exc.connection_invalidated:
        return True
    if isinstance(exc, IntegrityError):
        # Only reachable when a row appeared between the dedupe query and the insert.
        return is_duplicate_key(exc)
    return exc.orig is not None and bool(exc.orig.args) and exc.orig.args[0] in TRANSIENT_ERRNOS


def to_record(event: IngestionEvent, persisted_at: datetime) -> TrafficRecord:
    return TrafficRecord(
        event_id=event.event_id,
        service_id=event.service_id,
        title=event.title,
        value=event.value,
        source=event.source,
        created_by=None,
        observed_at=event.observed_at,
        persisted_at=persisted_at,
        threshold_applied=event.threshold_applied,
        is_anomaly=event.is_anomaly,
    )


class BatchWriter:
    """The only consumer of the buffer. It owns its state and the batch it holds."""

    def __init__(
        self,
        buffer: IngestionBuffer,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_max_size: int,
        flush_interval_s: float,
        retry_initial_s: float,
        retry_max_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._buffer = buffer
        self._session_factory = session_factory
        self._batch_max_size = batch_max_size
        self._flush_interval_s = flush_interval_s
        self._retry_initial_s = retry_initial_s
        self._retry_max_s = retry_max_s
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._closing = asyncio.Event()
        self._held: list[IngestionEvent] = []
        self.state = WriterState.IDLE
        self.retry_count = 0
        self.last_commit_at: datetime | None = None

    def stats(self) -> IngestionStats:
        held = len(self._held)
        in_flight = held if self.state is WriterState.IN_FLIGHT else 0
        total = self._buffer.total_uncommitted
        return IngestionStats(
            queued=total - held,
            in_flight=in_flight,
            retry_pending=held - in_flight,
            total_uncommitted=total,
            oldest_pending_age_s=self._buffer.oldest_pending_age_s(),
            last_commit_at=self.last_commit_at,
            retry_count=self.retry_count,
            writer_state=self.state,
        )

    def close(self) -> None:
        """Flush what is queued without waiting for the interval, then let run() return."""
        self._closing.set()

    async def run(self) -> None:
        try:
            while batch := await self._collect():
                if not await self._persist(batch):
                    await self._closing.wait()
                    return
        finally:
            self.state = WriterState.STOPPED

    async def _collect(self) -> list[IngestionEvent]:
        """Up to batch_max_size events, or fewer once flush_interval_s passes from the first."""
        first = await self._next_event(deadline=None)
        if first is None:
            return []
        batch = [first]
        deadline = self._clock() + self._flush_interval_s
        while len(batch) < self._batch_max_size:
            event = await self._next_event(deadline)
            if event is None:
                break
            batch.append(event)
        return batch

    async def _next_event(self, deadline: float | None) -> IngestionEvent | None:
        """The next event, or None at the deadline or, with the queue empty, on close."""
        if (event := self._buffer.take_nowait()) is not None:
            return event
        if self._closing.is_set():
            return None
        take = asyncio.ensure_future(self._buffer.take())
        waits = [take, asyncio.ensure_future(self._closing.wait())]
        if deadline is not None:
            waits.append(asyncio.ensure_future(self._sleep(max(0.0, deadline - self._clock()))))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waits:
                waiter.cancel()
            # A cancelled Queue.get() leaves its item in the queue, so nothing is lost here.
            await asyncio.gather(*waits, return_exceptions=True)
        return take.result() if not take.cancelled() else None

    async def _persist(self, batch: list[IngestionEvent]) -> bool:
        """Commit the batch, retrying transient failures. False after a permanent failure."""
        self._held = batch
        failures = 0
        while True:
            self.state = WriterState.IN_FLIGHT
            try:
                await self._commit(batch)
                break
            except Exception as exc:
                if not is_transient(exc):
                    self.state = WriterState.UNAVAILABLE
                    logger.error(
                        "writer stopped on a permanent error; batch kept in memory",
                        extra={"error": type(exc).__name__, "batch_size": len(batch)},
                        exc_info=exc,
                    )
                    await self._audit(
                        "writer.failed",
                        f"writer stopped: {type(exc).__name__}",
                        level="ERROR",
                        context={"error": type(exc).__name__, "batch_size": len(batch)},
                    )
                    return False
                delay = self._backoff(failures)
                failures += 1
                self.retry_count += 1
                self.state = WriterState.RETRY_WAIT
                logger.warning(
                    "batch commit failed; retrying",
                    extra={
                        "error": type(exc).__name__,
                        "attempt": failures,
                        "batch_size": len(batch),
                        "retry_in_s": round(delay, 3),
                    },
                )
                await self._sleep(delay)
        self._buffer.release(len(batch))
        self._held = []
        self.last_commit_at = utcnow()
        self.state = WriterState.IDLE
        if failures:
            logger.info(
                "writer recovered", extra={"failed_attempts": failures, "batch_size": len(batch)}
            )
            await self._audit(
                "writer.recovered",
                f"batch committed after {failures} failed attempts",
                level="INFO",
                context={"failed_attempts": failures, "batch_size": len(batch)},
            )
        return True

    def _backoff(self, failures: int) -> float:
        # Full jitter: uniform over [0, min(cap, initial * 2^n)].
        ceiling = min(self._retry_max_s, self._retry_initial_s * 2 ** min(failures, 32))
        return self._rng.uniform(0, ceiling)

    async def _commit(self, batch: list[IngestionEvent]) -> None:
        try:
            await self._insert(batch, skip_existing=False)
        except IntegrityError as exc:
            if not is_duplicate_key(exc):
                raise
            # An earlier commit may have landed with its ack lost: rows already there count
            # as persisted, so insert only the rest.
            await self._insert(batch, skip_existing=True)

    async def _insert(self, batch: list[IngestionEvent], *, skip_existing: bool) -> None:
        async with self._session_factory() as session:
            try:
                pending = batch
                if skip_existing:
                    ids = [event.event_id for event in batch]
                    existing = set(
                        await session.scalars(
                            select(TrafficRecord.event_id).where(TrafficRecord.event_id.in_(ids))
                        )
                    )
                    pending = [event for event in batch if event.event_id not in existing]
                persisted_at = utcnow()
                session.add_all(to_record(event, persisted_at) for event in pending)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _audit(
        self, event_type: str, message: str, *, level: str, context: dict[str, Any]
    ) -> None:
        """Best effort, in its own session: the database may be the thing that failed."""
        try:
            async with self._session_factory() as session:
                audit.record_event(
                    session, event_type, message, level=level, target_type="writer", context=context
                )
                await session.commit()
        except SQLAlchemyError as exc:
            logger.warning(
                "writer audit row not written",
                extra={"event_type": event_type, "error": type(exc).__name__},
            )
