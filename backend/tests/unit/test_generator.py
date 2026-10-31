import asyncio
import random
from datetime import UTC, datetime

import pytest

from app.ingestion.buffer import IngestionBuffer
from app.realtime.generator import Generator, ServiceSnapshot, sample

SERVICES = (
    ServiceSnapshot(1, "auth", "Auth API Traffic", 80),
    ServiceSnapshot(2, "orders", "Orders API Traffic", 150),
    ServiceSnapshot(3, "payment", "Payment API Traffic", 80),
)
OBSERVED_AT = datetime(2026, 10, 31, 11, 30, 6, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def broadcast(self, message: str) -> None:
        self.messages.append(message)


class ScriptedRandom(random.Random):
    def __init__(self, gauss: float, uniform: float) -> None:
        super().__init__(0)
        self._gauss, self._uniform = gauss, uniform

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        return self._gauss

    def random(self) -> float:
        return self._uniform


def generator(buffer: IngestionBuffer, recorder: Recorder, seed: int = 42) -> Generator:
    gen = Generator(
        buffer,
        recorder,
        None,  # type: ignore[arg-type]
        seed=seed,
        interval_s=1,
        reload_interval_s=5,
    )
    gen.services = SERVICES
    return gen


def values(gen: Generator, ticks: int) -> list[list[int]]:
    return [[event.value for event in gen.build_events(OBSERVED_AT)] for _ in range(ticks)]


def test_a_fixed_seed_reproduces_the_same_values() -> None:
    first = values(generator(IngestionBuffer(10), Recorder(), seed=7), ticks=50)
    again = values(generator(IngestionBuffer(10), Recorder(), seed=7), ticks=50)
    other = values(generator(IngestionBuffer(10), Recorder(), seed=8), ticks=50)

    assert first == again
    assert first != other


def test_each_tick_emits_one_event_per_service_with_its_threshold_snapshot() -> None:
    events = generator(IngestionBuffer(10), Recorder()).build_events(OBSERVED_AT)

    assert [(e.service_slug, e.threshold_applied) for e in events] == [
        ("auth", 80),
        ("orders", 150),
        ("payment", 80),
    ]
    assert all(e.is_anomaly == (e.value > e.threshold_applied) for e in events)
    assert all(e.observed_at == OBSERVED_AT for e in events)
    assert len({e.event_id for e in events}) == 3


@pytest.mark.parametrize(
    ("gauss", "uniform", "expected"),
    [
        (50.4, 0.5, 50),  # no spike, rounded to an integer
        (50.0, 0.01, 90),  # 5 % spike: x1.8
        (50.0, 0.05, 50),  # the spike draw is strictly below 5 %
        (-12.0, 0.5, 0),  # negative clamps to 0
    ],
)
def test_sample_spikes_rounds_and_clamps(gauss: float, uniform: float, expected: int) -> None:
    assert sample(ScriptedRandom(gauss, uniform), 60, 10) == expected


def test_nothing_is_emitted_before_thresholds_load() -> None:
    gen = generator(IngestionBuffer(10), Recorder())
    gen.services = None

    assert gen.build_events(OBSERVED_AT) == []


async def test_an_event_is_broadcast_only_after_the_buffer_accepts_it() -> None:
    buffer, recorder = IngestionBuffer(capacity=2), Recorder()
    gen = generator(buffer, recorder)

    ticking = asyncio.create_task(gen.tick())
    for _ in range(20):
        await asyncio.sleep(0)
    assert not ticking.done(), "the third event did not wait for capacity"
    assert len(recorder.messages) == 2, "an event was broadcast before accept() returned"

    buffer.take_nowait()
    buffer.release(1)
    await ticking
    assert len(recorder.messages) == 3
