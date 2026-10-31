"""WebSocket fan-out (ADR-003): one bounded queue and one sender task per connection."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

from starlette.websockets import WebSocket

from app.models import utcnow

logger = logging.getLogger(__name__)


class CloseCode(IntEnum):
    GOING_AWAY = 1001
    POLICY_VIOLATION = 1008  # the client could not keep up with its queue
    TRY_AGAIN_LATER = 1013  # connection cap reached, or the database is down
    UNAUTHORIZED = 4401  # missing, invalid or expired token


@dataclass(eq=False)
class _Connection:
    websocket: WebSocket
    expires_at: datetime
    queue: asyncio.Queue[str]
    sender: asyncio.Task[None] = field(init=False)
    # Set once teardown starts; the entry keeps its slot until this task ends.
    cleanup: asyncio.Task[None] | None = None


class ConnectionManager:
    def __init__(
        self,
        *,
        max_connections: int,
        queue_size: int,
        send_timeout_s: float,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._max_connections = max_connections
        self._queue_size = queue_size
        self._send_timeout_s = send_timeout_s
        self._now = now
        self._connections: set[_Connection] = set()

    @property
    def active_connections(self) -> int:
        """Sockets holding a slot, including those still being closed."""
        return len(self._connections)

    def broadcast(self, message: str) -> None:
        """Never awaits: a full queue drops that one client instead of slowing the caller."""
        for connection in list(self._connections):
            if connection.cleanup is not None:
                continue
            try:
                connection.queue.put_nowait(message)
            except asyncio.QueueFull:
                self._drop(connection, CloseCode.POLICY_VIOLATION)

    async def serve(self, websocket: WebSocket, expires_at: datetime, status: str) -> None:
        """Run an accepted, authenticated socket until the client leaves or is closed."""
        if len(self._connections) >= self._max_connections:
            await websocket.close(CloseCode.TRY_AGAIN_LATER)
            return
        connection = _Connection(websocket, expires_at, asyncio.Queue(self._queue_size))
        connection.queue.put_nowait(status)
        connection.sender = asyncio.create_task(self._send(connection))
        self._connections.add(connection)
        try:
            # Clients send nothing; receiving only notices when they leave.
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass
        finally:
            await self._drop(connection, None)

    async def _send(self, connection: _Connection) -> None:
        code: CloseCode | None
        try:
            while True:
                message = await connection.queue.get()
                if self._now() >= connection.expires_at:
                    code = CloseCode.UNAUTHORIZED
                    break
                async with asyncio.timeout(self._send_timeout_s):
                    await connection.websocket.send_text(message)
        except TimeoutError:
            code = CloseCode.POLICY_VIOLATION  # the client stopped reading
        except Exception as exc:
            # A failed send ends only this connection; the others, the writer and the buffer
            # never see it.
            logger.info("websocket send failed", extra={"error": type(exc).__name__})
            code = None
        self._drop(connection, code)

    def _drop(self, connection: _Connection, code: CloseCode | None) -> asyncio.Task[None]:
        """Tear the connection down from its own task: the sender may be stuck in a send."""
        if connection.cleanup is None:
            connection.cleanup = asyncio.create_task(self._close(connection, code))
        return connection.cleanup

    async def _close(self, connection: _Connection, code: CloseCode | None) -> None:
        try:
            connection.sender.cancel()
            await asyncio.wait([connection.sender])
            if code is not None:
                async with asyncio.timeout(self._send_timeout_s):
                    await connection.websocket.close(code)
        except Exception as exc:
            logger.info("websocket close failed", extra={"error": type(exc).__name__})
        finally:
            self._connections.discard(connection)

    async def close_all(self) -> None:
        closing = [self._drop(c, CloseCode.GOING_AWAY) for c in list(self._connections)]
        if closing:
            await asyncio.wait(closing)
