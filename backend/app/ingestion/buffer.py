"""Bounded ingestion buffer (ADR-001): capacity covers every accepted event until its commit."""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.models import RecordSource


@dataclass(frozen=True, slots=True)
class IngestionEvent:
    """An accepted observation. Frozen, so every retry writes the same identity and snapshot."""

    event_id: str
    service_id: int
    service_slug: str
    title: str
    value: int
    source: RecordSource
    observed_at: datetime
    threshold_applied: int
    is_anomaly: bool


class WriterState(StrEnum):
    IDLE = "IDLE"
    IN_FLIGHT = "IN_FLIGHT"
    RETRY_WAIT = "RETRY_WAIT"
    UNAVAILABLE = "UNAVAILABLE"
    STOPPED = "STOPPED"

    @property
    def halted(self) -> bool:
        """Nothing more gets persisted until the process restarts."""
        return self in (WriterState.UNAVAILABLE, WriterState.STOPPED)


@dataclass(frozen=True, slots=True)
class IngestionStats:
    queued: int
    in_flight: int
    retry_pending: int
    total_uncommitted: int
    oldest_pending_age_s: float | None
    last_commit_at: datetime | None
    retry_count: int
    writer_state: WriterState


class IngestionBuffer:
    """Reservations live in a semaphore; the queue only orders events for the writer."""

    def __init__(self, capacity: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = capacity
        self._clock = clock
        self._slots = asyncio.Semaphore(capacity)
        self._queue: asyncio.Queue[IngestionEvent] = asyncio.Queue(maxsize=capacity)
        # Accept times of uncommitted events, oldest first. The single writer commits in
        # FIFO order, so release(n) always confirms the n oldest. Unbounded on purpose: the
        # semaphore is the bound, and a maxlen would hide a breach by dropping accept times.
        self._uncommitted: deque[float] = deque()

    async def accept(self, event: IngestionEvent) -> None:
        """Wait for a reservation, then enqueue. The reservation is held until release()."""
        # No await between acquire() and put_nowait(): a cancellation lands inside acquire(),
        # which hands an already granted slot back, or not at all.
        await self._slots.acquire()
        self._queue.put_nowait(event)
        self._uncommitted.append(self._clock())
        assert len(self._uncommitted) <= self.capacity, "more uncommitted events than capacity"

    async def take(self) -> IngestionEvent:
        """The next event for the writer. Taking is not an ack: the reservation stays held."""
        return await self._queue.get()

    def take_nowait(self) -> IngestionEvent | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def release(self, count: int) -> None:
        """Free the reservations of the `count` oldest events after their commit is confirmed."""
        if count > len(self._uncommitted):
            raise ValueError(f"release({count}) exceeds {len(self._uncommitted)} uncommitted")
        for _ in range(count):
            self._uncommitted.popleft()
            self._slots.release()

    @property
    def total_uncommitted(self) -> int:
        return len(self._uncommitted)

    @property
    def is_full(self) -> bool:
        return len(self._uncommitted) >= self.capacity

    def oldest_pending_age_s(self) -> float | None:
        return self._clock() - self._uncommitted[0] if self._uncommitted else None
