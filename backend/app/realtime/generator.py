"""Simulator (architecture.md §5): per tick, one event per active service, accepted first."""

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.buffer import IngestionBuffer, IngestionEvent
from app.models import RecordSource, utcnow
from app.repositories import api_services
from app.schemas.realtime import TrafficObserved
from app.services.records import is_anomaly

logger = logging.getLogger(__name__)

# slug -> (mean, standard deviation) of requests per second.
DISTRIBUTIONS: dict[str, tuple[float, float]] = {
    "auth": (60, 10),
    "orders": (120, 15),
    "payment": (60, 10),
}
SPIKE_PROBABILITY = 0.05
SPIKE_FACTOR = 1.8


def sample(rng: random.Random, mean: float, stddev: float) -> int:
    value = rng.gauss(mean, stddev)
    if rng.random() < SPIKE_PROBABILITY:
        value *= SPIKE_FACTOR
    return max(0, round(value))


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    id: int
    slug: str
    title: str
    threshold: int


class Broadcaster(Protocol):
    def broadcast(self, message: str) -> None: ...


class Generator:
    def __init__(
        self,
        buffer: IngestionBuffer,
        broadcaster: Broadcaster,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        seed: int,
        interval_s: float,
        reload_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._buffer = buffer
        self._broadcaster = broadcaster
        self._session_factory = session_factory
        self._rng = random.Random(seed)
        self._interval_s = interval_s
        self._reload_interval_s = reload_interval_s
        self._clock = clock
        self._sleep = sleep
        # The in-process threshold map. None until the first load: emit nothing before it.
        self.services: tuple[ServiceSnapshot, ...] | None = None
        self.thresholds_loaded_at: datetime | None = None
        self.db_ok = False
        self.paused_s = 0.0

    async def reload_thresholds(self) -> None:
        """Replace the map from api_services; on failure keep the last good one."""
        try:
            async with self._session_factory() as session:
                rows = await api_services.list_all(session)
        except SQLAlchemyError as exc:
            self.db_ok = False
            logger.warning(
                "threshold reload failed; keeping the last loaded map",
                extra={"error": type(exc).__name__},
            )
            return
        self.services = tuple(
            ServiceSnapshot(row.id, row.slug, f"{row.display_name} Traffic", row.alert_threshold)
            for row in rows
            if row.is_active and row.slug in DISTRIBUTIONS
        )
        self.thresholds_loaded_at = utcnow()
        self.db_ok = True

    async def reload_forever(self) -> None:
        while True:
            await self._sleep(self._reload_interval_s)
            await self.reload_thresholds()

    def build_events(self, observed_at: datetime) -> list[IngestionEvent]:
        events = []
        for service in self.services or ():
            value = sample(self._rng, *DISTRIBUTIONS[service.slug])
            events.append(
                IngestionEvent(
                    event_id=str(uuid.uuid4()),
                    service_id=service.id,
                    service_slug=service.slug,
                    title=service.title,
                    value=value,
                    source=RecordSource.SIMULATOR,
                    observed_at=observed_at,
                    threshold_applied=service.threshold,
                    is_anomaly=is_anomaly(value, service.threshold),
                )
            )
        return events

    async def tick(self) -> None:
        for event in self.build_events(utcnow()):
            message = TrafficObserved.model_validate(event).model_dump_json()
            waited_from = self._clock()
            await self._buffer.accept(event)
            if (waited := self._clock() - waited_from) >= self._interval_s:
                self.paused_s += waited
                logger.warning(
                    "generator paused on a full buffer",
                    extra={"paused_s": round(waited, 1), "total_paused_s": round(self.paused_s, 1)},
                )
            self._broadcaster.broadcast(message)

    async def run(self) -> None:
        next_tick = self._clock()
        while True:
            await self.tick()
            next_tick += self._interval_s
            now = self._clock()
            # After a pause, resume on schedule: the missed ticks are a sampling gap, not a burst.
            next_tick = max(next_tick, now)
            await self._sleep(next_tick - now)
