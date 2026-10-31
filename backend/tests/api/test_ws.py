import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_pipeline
from app.db import get_session, make_engine
from app.main import app
from app.models import TrafficRecord, User
from app.pipeline import Pipeline
from app.realtime.manager import ConnectionManager
from tests.conftest import Identity

PATH = "/api/v1/ws/traffic"
Message = dict[str, Any]


@asynccontextmanager
async def connect(headers: dict[str, str] | None = None) -> AsyncIterator[asyncio.Queue[Message]]:
    """Drive the endpoint over raw ASGI, on the test's event loop and database fixtures."""
    to_app: asyncio.Queue[Message] = asyncio.Queue(maxsize=8)
    from_app: asyncio.Queue[Message] = asyncio.Queue(maxsize=32)
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "path": PATH,
        "raw_path": PATH.encode(),
        "root_path": "",
        "query_string": b"",
        "scheme": "ws",
        "server": ("test", 80),
        "client": ("test", 50000),
        "subprotocols": [],
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    await to_app.put({"type": "websocket.connect"})
    session = asyncio.create_task(app(scope, to_app.get, from_app.put))
    try:
        yield from_app
    finally:
        await to_app.put({"type": "websocket.disconnect", "code": 1000})
        await session


async def opened(received: asyncio.Queue[Message]) -> None:
    assert (await received.get())["type"] == "websocket.accept"


async def close_code(received: asyncio.Queue[Message]) -> int:
    message = await received.get()
    assert message["type"] == "websocket.close", message
    return message["code"]


async def next_json(received: asyncio.Queue[Message]) -> Message:
    message = await received.get()
    assert message["type"] == "websocket.send", message
    return json.loads(message["text"])


async def count_records(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(TrafficRecord)) or 0


async def test_no_token_is_closed_with_4401_and_sent_nothing(pipeline: Pipeline) -> None:
    async with connect() as received:
        await opened(received)
        assert await close_code(received) == 4401
    assert pipeline.manager.active_connections == 0


async def test_a_non_bearer_or_forged_token_is_closed_with_4401(
    pipeline: Pipeline, viewer: Identity
) -> None:
    forged = jwt.encode(
        {
            "sub": str(viewer.id),
            "role": "ADMIN",
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "iat": datetime.now(UTC),
            "jti": "forged",
        },
        "not-the-server-secret-but-32-bytes!",
        algorithm="HS256",
    )
    basic = viewer.headers["Authorization"].replace("Bearer", "Basic")
    for header in (basic, f"Bearer {forged}"):
        async with connect({"Authorization": header}) as received:
            await opened(received)
            assert await close_code(received) == 4401, header[:6]


async def test_a_deactivated_user_is_closed_with_4401(
    pipeline: Pipeline, viewer: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        user = await session.get(User, viewer.id)
        assert user is not None
        user.is_active = False
        await session.commit()

    async with connect(viewer.headers) as received:
        await opened(received)
        assert await close_code(received) == 4401


async def test_a_valid_token_gets_a_status_message_first(
    pipeline: Pipeline, viewer: Identity
) -> None:
    async with connect(viewer.headers) as received:
        await opened(received)
        assert await next_json(received) == {
            "type": "status",
            "state": "HEALTHY",
            "total_uncommitted": 0,
        }
        assert pipeline.manager.active_connections == 1
    assert pipeline.manager.active_connections == 0


async def test_connections_beyond_the_cap_are_closed_with_1013(
    pipeline: Pipeline, viewer: Identity
) -> None:
    pipeline.manager = ConnectionManager(max_connections=1, queue_size=10, send_timeout_s=5)
    async with connect(viewer.headers) as first:
        await opened(first)
        await next_json(first)
        async with connect(viewer.headers) as second:
            await opened(second)
            assert await close_code(second) == 1013
        assert pipeline.manager.active_connections == 1


async def test_the_cap_is_checked_before_the_user_is_reloaded(
    pipeline: Pipeline, viewer: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    pipeline.manager = ConnectionManager(max_connections=1, queue_size=10, send_timeout_s=5)
    async with connect(viewer.headers) as first:
        await opened(first)
        await next_json(first)
        async with session_factory() as session:
            user = await session.get(User, viewer.id)
            assert user is not None
            user.is_active = False
            await session.commit()

        async with connect(viewer.headers) as second:
            await opened(second)
            assert await close_code(second) == 1013, "a user reload would have answered 4401"


async def test_a_handshake_while_the_database_is_down_is_closed_with_1013(
    pipeline: Pipeline, viewer: Identity
) -> None:
    unreachable = make_engine("mysql+asyncmy://traffic:wrong@127.0.0.1:1/traffic_test")

    async def unreachable_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(unreachable) as session:
            yield session

    app.dependency_overrides[get_session] = unreachable_session
    try:
        async with connect(viewer.headers) as received:
            await opened(received)
            assert await close_code(received) == 1013
    finally:
        await unreachable.dispose()
    assert pipeline.manager.active_connections == 0


async def test_events_reach_the_client_before_they_are_committed(
    session_factory: async_sessionmaker[AsyncSession],
    services: dict[str, int],
    viewer: Identity,
) -> None:
    """WS-01: with the writer not running, the client has the events and MariaDB has none."""
    pipeline = Pipeline.create(session_factory, simulator_enabled=False)
    await pipeline.generator.reload_thresholds()
    app.dependency_overrides[get_pipeline] = lambda: pipeline

    async with connect(viewer.headers) as received:
        await opened(received)
        assert (await next_json(received))["type"] == "status"
        await pipeline.generator.tick()
        events = [await next_json(received) for _ in range(3)]

        assert [event["service_slug"] for event in events] == ["auth", "orders", "payment"]
        assert {event["type"] for event in events} == {"traffic.observed"}
        assert not any("id" in event or "persisted_at" in event for event in events)
        assert await count_records(session_factory) == 0
        assert pipeline.buffer.total_uncommitted == 3

    pipeline.writer.close()
    await pipeline.writer.run()
    assert await count_records(session_factory) == 3


class StatusPolls:
    """Stands in for the one-second status poll: the test runs one evaluation at a time."""

    def __init__(self) -> None:
        self.idle: asyncio.Queue[None] = asyncio.Queue()
        self.go: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self) -> None:
        self.idle.put_nowait(None)
        await self.go.get()

    async def step(self) -> None:
        """Let the loop evaluate the state once more and wait until it is idle again."""
        self.go.put_nowait(None)
        await self.idle.get()


async def test_a_state_change_is_pushed_once_to_connected_clients(
    session_factory: async_sessionmaker[AsyncSession],
    services: dict[str, int],
    viewer: Identity,
) -> None:
    polls = StatusPolls()
    pipeline = Pipeline.create(session_factory, simulator_enabled=False, status_poll=polls)
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    await pipeline.start()
    try:
        async with connect(viewer.headers) as received, asyncio.timeout(5):
            await polls.idle.get()
            await opened(received)
            assert (await next_json(received))["state"] == "HEALTHY"

            pipeline.writer.failed_attempts = 1  # a batch failed and awaits its retry
            await polls.step()
            assert (await next_json(received))["state"] == "DEGRADED"
            await polls.step()  # unchanged: nothing is pushed

            pipeline.writer.failed_attempts = 0
            await polls.step()
            assert (await next_json(received))["state"] == "HEALTHY"

            pipeline.generator.db_ok = False  # the last threshold reload failed
            await polls.step()
            assert (await next_json(received))["state"] == "DEGRADED"
            assert received.empty()
    finally:
        await pipeline.stop()
