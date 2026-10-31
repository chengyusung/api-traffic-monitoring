"""WebSocket receiver thread (ADR-003). It never calls st.* or touches st.session_state."""

import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, NamedTuple

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

MAX_EVENTS = 300
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 30.0
RECV_POLL_S = 1.0  # a blocked recv() wakes this often to notice stop()
CLOSE_UNAUTHORIZED = 4401

Event = dict[str, Any]


class ReceiverState(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    UNAUTHORIZED = "unauthorized"
    STOPPED = "stopped"


class Snapshot(NamedTuple):
    state: ReceiverState
    events: list[Event]
    status: Event | None
    last_message_at: float | None  # time.monotonic()


class Receiver:
    """A daemon thread that keeps one authenticated connection and buffers what arrives."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._events: deque[Event] = deque(maxlen=MAX_EVENTS)
        self._status: Event | None = None
        self._state = ReceiverState.CONNECTING
        self._last_message_at: float | None = None
        self._thread = threading.Thread(target=self._run, name="ws-receiver", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Ends the thread within RECV_POLL_S and forgets the token and every buffered event."""
        self._stop.set()
        with self._lock:
            self._token = ""
            self._events.clear()
            if self._state is not ReceiverState.UNAUTHORIZED:
                self._state = ReceiverState.STOPPED

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self._state, list(self._events), self._status, self._last_message_at)

    def _set_state(self, state: ReceiverState) -> None:
        with self._lock:
            if not self._stop.is_set():
                self._state = state

    def _run(self) -> None:
        backoff = BACKOFF_INITIAL_S
        while not self._stop.is_set():
            try:
                with connect(
                    self._url,
                    additional_headers={"Authorization": f"Bearer {self._token}"},
                    open_timeout=5,
                    close_timeout=2,
                ) as websocket:
                    self._set_state(ReceiverState.CONNECTED)
                    while not self._stop.is_set():
                        try:
                            raw = websocket.recv(timeout=RECV_POLL_S)
                        except TimeoutError:
                            continue
                        self._handle(raw)
                        backoff = BACKOFF_INITIAL_S
            except ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code == CLOSE_UNAUTHORIZED:
                    self._set_state(ReceiverState.UNAUTHORIZED)
                    return
            except (OSError, TimeoutError, WebSocketException):
                pass  # unreachable, refused or rejected: retry below
            if self._stop.is_set():
                return
            self._set_state(ReceiverState.RECONNECTING)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        with self._lock:
            if self._stop.is_set():
                return
            self._last_message_at = time.monotonic()
            if message.get("type") == "status":
                self._status = message
            elif message.get("type") == "traffic.observed":
                self._events.append(message)


def merge_window(window: dict[str, Event], events: list[Event], span: timedelta) -> None:
    """Add unseen events (by event_id) and drop those older than `span` before the newest.

    Gaps stay gaps: nothing is filled in for seconds with no event.
    """
    for event in events:
        window.setdefault(event["event_id"], event)
    if not window:
        return
    observed = {key: datetime.fromisoformat(event["observed_at"]) for key, event in window.items()}
    cutoff = max(observed.values()) - span
    for key, at in observed.items():
        if at < cutoff:
            del window[key]
