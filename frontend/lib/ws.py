"""WebSocket receiver thread (ADR-003). It never calls st.* or touches st.session_state."""

import json
import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, NamedTuple

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

logger = logging.getLogger(__name__)

# 120 s of chart at 3 events/s is 360 events; 400 leaves headroom between two page reads.
MAX_EVENTS = 400
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 30.0
STABLE_AFTER_S = 10.0  # only a connection that lasted this long resets the back-off
RECV_POLL_S = 1.0  # a blocked recv() wakes this often to notice stop()
OPEN_TIMEOUT_S = 5.0
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


class Backoff:
    """Full-jitter reconnect waits under ceilings of 1, 2, 4 … 30 s."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._failures = 0

    def next_wait(self, connected_for_s: float | None) -> float:
        """The wait before the next attempt. A server that accepts and drops at once must not
        reset the sequence, so only a connection that stayed up STABLE_AFTER_S does."""
        if connected_for_s is not None and connected_for_s >= STABLE_AFTER_S:
            self._failures = 0
        ceiling = min(BACKOFF_MAX_S, BACKOFF_INITIAL_S * 2 ** min(self._failures, 32))
        self._failures += 1
        return self._rng.uniform(0, ceiling)


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

    @property
    def crashed(self) -> bool:
        """The thread ended without stop() or a 4401, so nothing is receiving any more."""
        with self._lock:
            ended_on_purpose = self._state in (ReceiverState.UNAUTHORIZED, ReceiverState.STOPPED)
        return not self._thread.is_alive() and not ended_on_purpose

    def stop(self) -> None:
        """Forgets the token and every buffered event, and tells the thread to end. It ends
        within RECV_POLL_S, or OPEN_TIMEOUT_S if a connection attempt is in progress."""
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
        backoff = Backoff()
        while not self._stop.is_set():
            connected_at: float | None = None
            try:
                with connect(
                    self._url,
                    additional_headers={"Authorization": f"Bearer {self._token}"},
                    open_timeout=OPEN_TIMEOUT_S,
                    close_timeout=2,
                ) as websocket:
                    connected_at = time.monotonic()
                    self._set_state(ReceiverState.CONNECTED)
                    while not self._stop.is_set():
                        try:
                            raw = websocket.recv(timeout=RECV_POLL_S)
                        except TimeoutError:
                            continue
                        self._handle(raw)
            except ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code == CLOSE_UNAUTHORIZED:
                    self._set_state(ReceiverState.UNAUTHORIZED)
                    return
            except Exception as exc:
                # Unreachable, refused, rejected, or a message this code cannot read: any of
                # them would otherwise end the thread for good. The type only: no token.
                logger.warning("websocket receiver error, reconnecting: %s", type(exc).__name__)
            if self._stop.is_set():
                return
            self._set_state(ReceiverState.RECONNECTING)
            connected_for_s = None if connected_at is None else time.monotonic() - connected_at
            self._stop.wait(backoff.next_wait(connected_for_s))

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
