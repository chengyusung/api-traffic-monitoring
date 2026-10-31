"""Simulator, buffer, writer and WebSocket fan-out: built, started and stopped together."""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.ingestion.buffer import IngestionBuffer, WriterState
from app.ingestion.writer import BatchWriter
from app.realtime.generator import Generator
from app.realtime.manager import ConnectionManager
from app.schemas.realtime import HealthState, StatusMessage

logger = logging.getLogger(__name__)

STATUS_POLL_S = 1.0


def evaluate(
    *,
    ready: bool,
    db_ok: bool,
    writer_state: WriterState,
    retrying: bool,
    stalled: bool,
    buffer_full: bool,
) -> HealthState:
    """architecture.md §9. `retrying` holds from a batch's first failure until its commit;
    `stalled` means no commit for two flush intervals, whatever the writer's state says."""
    writer_stopped = writer_state in (WriterState.UNAVAILABLE, WriterState.STOPPED)
    if not ready or writer_stopped or (buffer_full and (retrying or stalled)):
        return HealthState.UNAVAILABLE
    if not db_ok or retrying:
        return HealthState.DEGRADED
    return HealthState.HEALTHY


def _log_crash(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("pipeline task crashed", extra={"task": task.get_name()}, exc_info=exc)


@dataclass(eq=False)
class Pipeline:
    buffer: IngestionBuffer
    writer: BatchWriter
    generator: Generator
    manager: ConnectionManager
    simulator_enabled: bool
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        simulator_enabled: bool = settings.simulator_enabled,
    ) -> "Pipeline":
        buffer = IngestionBuffer(settings.buffer_capacity)
        manager = ConnectionManager(
            max_connections=settings.ws_max_connections,
            queue_size=settings.ws_per_connection_queue,
        )
        return cls(
            buffer=buffer,
            writer=BatchWriter(
                buffer,
                session_factory,
                batch_max_size=settings.batch_max_size,
                flush_interval_s=settings.flush_interval_seconds,
                retry_initial_s=settings.retry_initial_backoff_seconds,
                retry_max_s=settings.retry_max_backoff_seconds,
                commit_timeout_s=settings.commit_timeout_seconds,
            ),
            generator=Generator(
                buffer,
                manager,
                session_factory,
                seed=settings.simulator_seed,
                interval_s=settings.simulator_interval_seconds,
                reload_interval_s=settings.threshold_reload_seconds,
            ),
            manager=manager,
            simulator_enabled=simulator_enabled,
        )

    async def start(self) -> None:
        """Load thresholds, then start the writer, then the generator."""
        await self.generator.reload_thresholds()
        self._spawn("writer", self.writer.run())
        self._spawn("thresholds", self.generator.reload_forever())
        if self.simulator_enabled:
            self._spawn("generator", self.generator.run())
        self._spawn("status", self._push_state_changes())

    async def stop(self) -> None:
        """Stop the generator, flush the writer within the timeout, then close the sockets."""
        for name in ("generator", "thresholds", "status"):
            if task := self._tasks.get(name):
                task.cancel()
                await asyncio.wait([task])
        self.writer.close()
        if writer := self._tasks.get("writer"):
            # asyncio.wait never re-raises the writer's exception (_log_crash logs it), so the
            # sockets and the engine are still closed after a crash.
            await asyncio.wait([writer], timeout=settings.shutdown_flush_timeout_seconds)
            writer.cancel()
            await asyncio.wait([writer])
        abandoned = self.buffer.total_uncommitted
        level = logging.WARNING if abandoned else logging.INFO
        logger.log(level, "ingestion stopped", extra={"abandoned_events": abandoned})
        await self.manager.close_all()

    def state(self, *, db_ok: bool) -> HealthState:
        ready = (
            self.generator.services is not None
            and bool(self._tasks)
            and not any(task.done() for task in self._tasks.values())
        )
        return evaluate(
            ready=ready,
            db_ok=db_ok,
            writer_state=self.writer.state,
            retrying=self.writer.retrying,
            stalled=self.writer.stalled,
            buffer_full=self.buffer.is_full,
        )

    def status_message(self) -> str:
        """For WebSocket clients. DB health is the last threshold reload, not a fresh probe."""
        return StatusMessage(
            state=self.state(db_ok=self.generator.db_ok),
            total_uncommitted=self.buffer.total_uncommitted,
        ).model_dump_json()

    async def _push_state_changes(self) -> None:
        last: HealthState | None = None
        while True:
            state = self.state(db_ok=self.generator.db_ok)
            if state is not last:
                last = state
                self.manager.broadcast(self.status_message())
            await asyncio.sleep(STATUS_POLL_S)

    def _spawn(self, name: str, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work, name=name)
        task.add_done_callback(_log_crash)
        self._tasks[name] = task
