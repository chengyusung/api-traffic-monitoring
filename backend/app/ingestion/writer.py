"""Batch writer (ADR-001): one transaction per batch, capacity released only after commit."""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import database_unavailable
from app.db import is_duplicate_key
from app.ingestion.buffer import IngestionBuffer, IngestionEvent, IngestionStats, WriterState
from app.models import TrafficRecord, utcnow
from app.services import audit

logger = logging.getLogger(__name__)


def is_transient(exc: Exception) -> bool:
    """What a later attempt can fix is retried forever; anything else stops the writer."""
    # A duplicate is only reachable when a row appeared between the dedupe query and the insert.
    return database_unavailable(exc) or (isinstance(exc, IntegrityError) and is_duplicate_key(exc))


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


async def wait_first(*futures: asyncio.Future[Any]) -> None:
    """Return once one future is done; cancel the others and wait for them to finish."""
    try:
        await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for future in futures:
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)


def records(events: list[IngestionEvent]) -> list[TrafficRecord]:
    persisted_at = utcnow()
    return [to_record(event, persisted_at) for event in events]


async def missing_only(session: AsyncSession, batch: list[IngestionEvent]) -> list[IngestionEvent]:
    """The events of `batch` whose event_id is not stored yet."""
    ids = [event.event_id for event in batch]
    stored = set(
        await session.scalars(select(TrafficRecord.event_id).where(TrafficRecord.event_id.in_(ids)))
    )
    return [event for event in batch if event.event_id not in stored]


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
        commit_timeout_s: float,
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
        self._commit_timeout_s = commit_timeout_s
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._closing = asyncio.Event()
        self._held: list[IngestionEvent] = []
        self.state = WriterState.IDLE
        self.retry_count = 0
        self.failed_attempts = 0
        self.last_commit_at: datetime | None = None

    @property
    def retrying(self) -> bool:
        """The held batch failed at least once: still true while the next attempt is in flight."""
        return self.failed_attempts > 0

    @property
    def stalled(self) -> bool:
        """An event has waited two flush intervals without a commit."""
        age = self._buffer.oldest_pending_age_s()
        return age is not None and age > 2 * self._flush_interval_s

    def stats(self) -> IngestionStats:
        """Each count is read where its events are, not derived from the others. The batch in
        the writer's hands is in_flight while it is collected, committed, or kept after a
        permanent error, and retry_pending only during a back-off."""
        held = len(self._held)
        backing_off = self.state is WriterState.RETRY_WAIT
        return IngestionStats(
            queued=self._buffer.queued,
            in_flight=0 if backing_off else held,
            retry_pending=held if backing_off else 0,
            total_uncommitted=self._buffer.total_uncommitted,
            oldest_pending_age_s=self._buffer.oldest_pending_age_s(),
            last_commit_at=self.last_commit_at,
            retry_count=self.retry_count,
            writer_state=self.state,
        )

    def close(self) -> None:
        """Flush what is queued without waiting for the interval, then let run() return.

        A batch in back-off gets one immediate attempt; after close() no batch is retried.
        """
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
        self._held = [first]
        deadline = self._clock() + self._flush_interval_s
        while len(self._held) < self._batch_max_size:
            event = await self._next_event(deadline)
            if event is None:
                break
            self._held.append(event)
        return self._held

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
        await wait_first(*waits)
        if not take.cancelled():
            return take.result()
        # A cancelled Queue.get() leaves its item queued; one put during the cancel would
        # otherwise stay there, reserved, after run() returns.
        return self._buffer.take_nowait()

    async def _persist(self, batch: list[IngestionEvent]) -> bool:
        """Commit the batch, retrying transient failures. False after a permanent failure, or
        when the last attempt after close() fails."""
        while True:
            self.state = WriterState.IN_FLIGHT
            last_attempt = self._closing.is_set()
            try:
                # A hung database becomes a transient TimeoutError, retried on a fresh session.
                async with asyncio.timeout(self._commit_timeout_s):
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
                delay = self._backoff(self.failed_attempts)
                self.failed_attempts += 1
                self.retry_count += 1
                if last_attempt:
                    logger.warning(
                        "batch abandoned at shutdown after its last attempt",
                        extra={
                            "error": type(exc).__name__,
                            "abandoned_events": self._buffer.total_uncommitted,
                        },
                    )
                    return False
                self.state = WriterState.RETRY_WAIT
                logger.warning(
                    "batch commit failed; retrying",
                    extra={
                        "error": type(exc).__name__,
                        "attempt": self.failed_attempts,
                        "batch_size": len(batch),
                        "retry_in_s": round(delay, 3),
                    },
                )
                await wait_first(
                    asyncio.ensure_future(self._sleep(delay)),
                    asyncio.ensure_future(self._closing.wait()),
                )
        self._buffer.release(len(batch))
        self._held = []
        self.last_commit_at = utcnow()
        self.state = WriterState.IDLE
        failures, self.failed_attempts = self.failed_attempts, 0
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
            async with self._session_factory() as session, session.begin():
                session.add_all(records(batch))
        except IntegrityError as exc:
            if not is_duplicate_key(exc):
                raise
            # An earlier commit may have landed with its ack lost: rows already there count
            # as persisted, so insert only the rest.
            async with self._session_factory() as session, session.begin():
                session.add_all(records(await missing_only(session, batch)))

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
        except Exception as exc:
            logger.warning(
                "writer audit row not written",
                extra={"event_type": event_type, "error": type(exc).__name__},
            )
