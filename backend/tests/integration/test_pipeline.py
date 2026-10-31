import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.pipeline import Pipeline


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
