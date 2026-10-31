"""WebSocket fan-out (ADR-003): one bounded queue and one sender task per connection."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
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
    close_code: CloseCode | None = None


class ConnectionManager:
    def __init__(
        self,
        *,
        max_connections: int,
        queue_size: int,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._max_connections = max_connections
        self._queue_size = queue_size
        self._now = now
        self._connections: dict[_Connection, asyncio.Task[None]] = {}

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    def broadcast(self, message: str) -> None:
        """Never awaits: a full queue drops that one client instead of slowing the caller."""
        for connection in list(self._connections):
            try:
                connection.queue.put_nowait(message)
            except asyncio.QueueFull:
                self._connections.pop(connection, None)
                connection.close_code = CloseCode.POLICY_VIOLATION

    async def serve(self, websocket: WebSocket, expires_at: datetime, status: str) -> None:
        """Run an accepted, authenticated socket until the client leaves or is closed."""
        if len(self._connections) >= self._max_connections:
            await websocket.close(CloseCode.TRY_AGAIN_LATER)
            return
        connection = _Connection(websocket, expires_at, asyncio.Queue(self._queue_size))
        connection.queue.put_nowait(status)
        self._connections[connection] = asyncio.create_task(self._send(connection))
        sender = self._connections[connection]
        try:
            # Clients send nothing; receiving only notices when they leave.
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass
        finally:
            self._connections.pop(connection, None)
            sender.cancel()

    async def _send(self, connection: _Connection) -> None:
        try:
            while True:
                message = await connection.queue.get()
                if connection.close_code is None and self._now() >= connection.expires_at:
                    connection.close_code = CloseCode.UNAUTHORIZED
                if connection.close_code is not None:
                    break
                await connection.websocket.send_text(message)
            self._connections.pop(connection, None)
            await connection.websocket.close(connection.close_code)
        except Exception as exc:
            # A failed send ends only this connection; the others, the writer and the buffer
            # never see it.
            self._connections.pop(connection, None)
            logger.info("websocket send failed", extra={"error": type(exc).__name__})

    async def close_all(self) -> None:
        connections = list(self._connections.items())
        self._connections.clear()
        for connection, sender in connections:
            sender.cancel()
            await asyncio.wait([sender])
            with suppress(Exception):
                await connection.websocket.close(CloseCode.GOING_AWAY)
