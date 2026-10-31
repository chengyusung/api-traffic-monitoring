import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import main
from app.ingestion.buffer import IngestionBuffer, WriterState
from app.pipeline import Pipeline
from app.realtime.generator import Generator


async def test_stop_still_closes_the_sockets_after_the_writer_crashed(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = Pipeline.create(session_factory, simulator_enabled=False)

    async def crash() -> None:
        raise RuntimeError("a bug in the writer")

    closed: list[bool] = []
    close_all = pipeline.manager.close_all

    async def recording_close_all() -> None:
        closed.append(True)
        await close_all()

    monkeypatch.setattr(pipeline.writer, "run", crash)
    monkeypatch.setattr(pipeline.manager, "close_all", recording_close_all)
    await pipeline.start()
    await asyncio.sleep(0)  # one loop step: the writer task runs and raises

    await pipeline.stop()

    assert closed == [True]


async def test_a_failed_threshold_reload_keeps_the_last_map(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    failure: list[Exception] = []

    def sessions() -> AsyncSession:
        if failure:
            raise failure[0]
        return session_factory()

    gen = Generator(
        IngestionBuffer(10),
        SimpleNamespace(broadcast=lambda message: None),
        sessions,  # type: ignore[arg-type]
        writer_state=lambda: WriterState.IDLE,
        seed=42,
        interval_s=1,
        reload_interval_s=5,
    )
    await gen.reload_thresholds()
    loaded, loaded_at = gen.services, gen.thresholds_loaded_at
    assert loaded is not None and {service.slug for service in loaded} == set(services)

    failure.append(OSError("network is unreachable"))  # not an SQLAlchemyError
    await gen.reload_thresholds()

    assert (gen.services, gen.thresholds_loaded_at) == (loaded, loaded_at)
    assert not gen.db_ok


async def test_a_failed_startup_still_disposes_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed: list[bool] = []

    async def dispose() -> None:
        disposed.append(True)

    async def failing_start(self: Pipeline) -> None:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(Pipeline, "start", failing_start)

    with pytest.raises(RuntimeError, match="startup failed"):
        async with main.lifespan(main.app):
            pass

    assert disposed == [True]
