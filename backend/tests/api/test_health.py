from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_pipeline
from app.db import get_session, make_engine
from app.ingestion.buffer import WriterState
from app.main import app
from app.models import utcnow
from app.pipeline import Pipeline, evaluate
from app.schemas.realtime import HealthState

READINESS_FIELDS = {
    "state",
    "queued",
    "in_flight",
    "retry_pending",
    "total_uncommitted",
    "retry_count",
    "oldest_pending_age_s",
    "last_commit_at",
    "writer_state",
    "active_connections",
}


async def test_live_returns_200(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


async def test_ready_reports_the_pipelines_real_counters(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    services: dict[str, int],
) -> None:
    """The writer is not running, so no flush interval can move the events mid-test."""
    pipeline = Pipeline.create(session_factory, simulator_enabled=False)
    await pipeline.generator.reload_thresholds()
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    for event in pipeline.generator.build_events(utcnow()):
        await pipeline.buffer.accept(event)

    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body.keys() == READINESS_FIELDS
    assert body["state"] == "UNAVAILABLE", "the writer task is not running"
    assert (body["queued"], body["in_flight"], body["retry_pending"]) == (3, 0, 0)
    assert body["total_uncommitted"] == 3
    assert body["oldest_pending_age_s"] >= 0
    assert (body["writer_state"], body["retry_count"], body["last_commit_at"]) == ("IDLE", 0, None)
    assert body["active_connections"] == 0


async def test_ready_is_unavailable_until_the_pipeline_starts(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    app.dependency_overrides[get_pipeline] = lambda: Pipeline.create(session_factory)

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json().keys() == READINESS_FIELDS
    assert response.json()["state"] == "UNAVAILABLE"


async def test_ready_is_degraded_503_with_full_body_when_db_is_unreachable(
    client: AsyncClient, pipeline: Pipeline
) -> None:
    unreachable = make_engine("mysql+asyncmy://traffic:wrong@127.0.0.1:1/traffic_test")

    async def unreachable_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(unreachable) as session:
            yield session

    app.dependency_overrides[get_session] = unreachable_session
    response = await client.get("/health/ready")
    await unreachable.dispose()

    assert response.status_code == 503
    body = response.json()
    assert body.keys() == READINESS_FIELDS
    assert body["state"] == "DEGRADED"


@pytest.mark.parametrize(
    ("ready", "db_ok", "writer_state", "retrying", "stalled", "buffer_full", "expected"),
    [
        (True, True, WriterState.IDLE, False, False, False, HealthState.HEALTHY),
        (True, True, WriterState.IN_FLIGHT, False, False, True, HealthState.HEALTHY),
        (True, True, WriterState.IN_FLIGHT, False, True, False, HealthState.HEALTHY),
        (True, False, WriterState.IDLE, False, False, False, HealthState.DEGRADED),
        (True, True, WriterState.RETRY_WAIT, True, False, False, HealthState.DEGRADED),
        (True, True, WriterState.IN_FLIGHT, True, False, False, HealthState.DEGRADED),
        (True, False, WriterState.RETRY_WAIT, True, True, True, HealthState.UNAVAILABLE),
        (True, True, WriterState.IN_FLIGHT, True, True, True, HealthState.UNAVAILABLE),
        # A hung commit: no retry yet, but nothing committed for two flush intervals.
        (True, True, WriterState.IN_FLIGHT, False, True, True, HealthState.UNAVAILABLE),
        (False, True, WriterState.IDLE, False, False, False, HealthState.UNAVAILABLE),
        (True, True, WriterState.UNAVAILABLE, False, False, False, HealthState.UNAVAILABLE),
        (True, True, WriterState.STOPPED, False, False, False, HealthState.UNAVAILABLE),
    ],
)
def test_health_state_follows_architecture_section_9(
    ready: bool,
    db_ok: bool,
    writer_state: WriterState,
    retrying: bool,
    stalled: bool,
    buffer_full: bool,
    expected: HealthState,
) -> None:
    assert (
        evaluate(
            ready=ready,
            db_ok=db_ok,
            writer_state=writer_state,
            retrying=retrying,
            stalled=stalled,
            buffer_full=buffer_full,
        )
        is expected
    )
