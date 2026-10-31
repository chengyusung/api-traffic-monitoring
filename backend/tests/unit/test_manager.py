import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.websockets import WebSocket

from app.realtime.manager import ConnectionManager

Message = dict[str, Any]
EXPIRES_AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SEND_TIMEOUT_S = 0.2  # real seconds: asyncio.timeout runs on the event loop's clock


class Client:
    """The ASGI side of one socket. Messages whose type is in `stuck_on` are recorded and then
    never return, like a client that stopped reading."""

    def __init__(self, *, stuck_on: frozenset[str] = frozenset()) -> None:
        self.stuck_on = stuck_on
        self.sent: list[Message] = []
        self.inbound: asyncio.Queue[Message] = asyncio.Queue()
        self.inbound.put_nowait({"type": "websocket.connect"})
        self._changed = asyncio.Condition()

    async def receive(self) -> Message:
        return await self.inbound.get()

    async def send(self, message: Message) -> None:
        async with self._changed:
            self.sent.append(message)
            self._changed.notify_all()
        if message["type"] in self.stuck_on:
            await asyncio.Event().wait()

    async def wait_for(self, predicate: Callable[[list[Message]], bool]) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: predicate(self.sent))

    def leave(self) -> None:
        self.inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})

    def closed_with(self) -> int | None:
        return next((m["code"] for m in self.sent if m["type"] == "websocket.close"), None)


async def accepted(client: Client) -> WebSocket:
    scope = {"type": "websocket", "path": "/", "headers": [], "query_string": b""}
    websocket = WebSocket(scope, client.receive, client.send)
    await websocket.accept()
    return websocket


def manager(**overrides: Any) -> ConnectionManager:
    options: dict[str, Any] = {
        "max_connections": 10,
        "queue_size": 2,
        "send_timeout_s": SEND_TIMEOUT_S,
        "now": lambda: EXPIRES_AT - timedelta(hours=1),
    }
    return ConnectionManager(**(options | overrides))


async def test_a_client_that_overflows_its_queue_is_closed_with_1008_despite_a_stuck_send() -> None:
    # The send timeout is far away: only the overflow can close this socket within the test.
    fanout, client = manager(send_timeout_s=60), Client(stuck_on=frozenset({"websocket.send"}))
    serving = asyncio.create_task(fanout.serve(await accepted(client), EXPIRES_AT, "status"))
    async with asyncio.timeout(5):
        await client.wait_for(lambda sent: any(m.get("text") == "status" for m in sent))

        # The sender is now stuck in send_text; queue_size 2, so the third message overflows.
        for n in range(3):
            fanout.broadcast(f"event {n}")
        assert fanout.active_connections == 1, "the slot was freed before the socket closed"

        await client.wait_for(lambda sent: any(m["type"] == "websocket.close" for m in sent))
        assert client.closed_with() == 1008

        fanout.broadcast("after the drop")
        assert not any(m.get("text") == "after the drop" for m in client.sent)
        client.leave()
        await serving
    assert fanout.active_connections == 0


async def test_a_send_that_never_returns_times_out_and_frees_the_slot() -> None:
    client = Client(stuck_on=frozenset({"websocket.send", "websocket.close"}))
    fanout = manager()
    serving = asyncio.create_task(fanout.serve(await accepted(client), EXPIRES_AT, "status"))

    async with asyncio.timeout(5):
        await client.wait_for(lambda sent: any(m["type"] == "websocket.close" for m in sent))
        assert client.closed_with() == 1008, "only the send timeout can have closed it"
        assert fanout.active_connections == 1, "the slot was freed while the close is pending"
        client.leave()
        await serving  # returns once the stuck close has hit its own timeout
    assert fanout.active_connections == 0
