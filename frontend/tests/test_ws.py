"""Run from frontend/: uv run python -m unittest tests.test_ws"""

import json
import os
import random
import threading
import unittest
from unittest import mock

from websockets.sync.server import Server, ServerConnection, serve

os.environ.setdefault("API_WS_URL", "ws://127.0.0.1:9/unused")

from lib import session, ws  # noqa: E402

STATUS = {"type": "status", "state": "HEALTHY", "total_uncommitted": 0}


class CeilingJitter(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return b


class BackoffTest(unittest.TestCase):
    def test_ceilings_double_from_one_second_up_to_thirty(self) -> None:
        backoff = ws.Backoff(CeilingJitter())
        self.assertEqual([backoff.next_wait(None) for _ in range(7)], [1, 2, 4, 8, 16, 30, 30])

    def test_waits_are_jittered_below_the_ceiling(self) -> None:
        backoff = ws.Backoff(random.Random(0))
        waits = [backoff.next_wait(None) for _ in range(6)]
        pairs = list(zip(waits, [1, 2, 4, 8, 16, 30], strict=True))
        self.assertTrue(all(0 <= wait <= ceiling for wait, ceiling in pairs))
        self.assertTrue(any(wait < ceiling for wait, ceiling in pairs), "no jitter")

    def test_only_a_connection_that_stayed_up_resets_the_sequence(self) -> None:
        backoff = ws.Backoff(CeilingJitter())
        self.assertEqual([backoff.next_wait(None), backoff.next_wait(None)], [1, 2])
        self.assertEqual(backoff.next_wait(ws.STABLE_AFTER_S - 1), 4, "accept-then-drop")
        self.assertEqual(backoff.next_wait(ws.STABLE_AFTER_S), 1)


class FlakyServer:
    """First connection sends a JSON array, which the receiver cannot read; later ones a
    status message."""

    def __init__(self) -> None:
        self.connections = 0
        self.server: Server = serve(self._handle, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"

    def _handle(self, connection: ServerConnection) -> None:
        self.connections += 1
        connection.send("[]" if self.connections == 1 else json.dumps(STATUS))
        try:
            connection.recv()
        except Exception:
            pass


class ReceiverTest(unittest.TestCase):
    def test_an_unreadable_message_reconnects_instead_of_ending_the_thread(self) -> None:
        server = FlakyServer()
        self.addCleanup(server.server.shutdown)
        receiver = ws.Receiver(server.url, "token")
        with mock.patch.object(ws, "BACKOFF_INITIAL_S", 0.01):
            receiver.start()
            self.addCleanup(receiver.stop)
            for _ in range(500):
                if receiver.snapshot().status is not None:
                    break
                threading.Event().wait(0.01)

        self.assertEqual(receiver.snapshot().status, STATUS)
        self.assertFalse(receiver.crashed)
        self.assertGreaterEqual(server.connections, 2)


class CrashingReceiver(ws.Receiver):
    def _run(self) -> None:
        raise SystemExit  # a thread death that no except clause in _run sees


class IdleReceiver(ws.Receiver):
    def _run(self) -> None:
        self._stop.wait()


class SessionReceiverTest(unittest.TestCase):
    def receivers(self, cls: type[ws.Receiver]) -> tuple[ws.Receiver, ws.Receiver]:
        """Two page reruns in one session."""
        with (
            mock.patch.object(session.st, "session_state", {"token": "token"}),
            mock.patch.object(session, "Receiver", cls),
        ):
            first = session.receiver()
            first._thread.join(timeout=0.2)
            second = session.receiver()
        self.addCleanup(first.stop)
        self.addCleanup(second.stop)
        return first, second

    def test_a_crashed_receiver_is_replaced(self) -> None:
        first, second = self.receivers(CrashingReceiver)
        self.assertTrue(first.crashed)
        self.assertIsNot(second, first)

    def test_a_running_receiver_is_reused(self) -> None:
        first, second = self.receivers(IdleReceiver)
        self.assertIs(second, first)

    def test_a_receiver_stopped_on_purpose_is_not_crashed(self) -> None:
        receiver = ws.Receiver("ws://127.0.0.1:9/unused", "token")
        receiver.start()
        receiver.stop()
        receiver._thread.join(timeout=10)
        self.assertFalse(receiver._thread.is_alive())
        self.assertFalse(receiver.crashed)


if __name__ == "__main__":
    unittest.main()
