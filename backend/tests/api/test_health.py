from collections.abc import AsyncIterator

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, make_engine
from app.main import app

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
ZERO_COUNTERS = ("queued", "in_flight", "retry_pending", "total_uncommitted", "retry_count")


async def test_live_returns_200(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


async def test_ready_is_healthy_with_every_counter_field(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body.keys() == READINESS_FIELDS
    assert body["state"] == "HEALTHY"
    assert all(body[name] == 0 for name in ZERO_COUNTERS)
    assert body["writer_state"] == "NOT_STARTED"


async def test_ready_is_degraded_503_with_full_body_when_db_is_unreachable(
    client: AsyncClient,
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
