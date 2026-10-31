from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_session, make_engine
from app.ingestion.buffer import WriterState
from app.main import app
from app.models import AuditEvent, SystemLog
from app.pipeline import Pipeline
from app.services import admin as admin_service
from app.services import audit
from tests.api.test_records import insert_record
from tests.conftest import Identity

ADMIN = "/api/v1/admin"
LOGS = f"{ADMIN}/logs"
T0 = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
WINDOW = {"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat()}


@pytest.fixture(autouse=True)
def no_cached_table_counts() -> None:
    """Each test counts its own rows, never another test's cached counts."""
    admin_service._table_counts.clear()


async def add_log(
    session_factory: async_sessionmaker[AsyncSession],
    created_at: datetime,
    event_type: AuditEvent = AuditEvent.RECORD_DELETED,
    level: str = "INFO",
) -> int:
    async with session_factory() as session:
        row = SystemLog(level=level, event_type=event_type, message="seeded", created_at=created_at)
        session.add(row)
        await session.commit()
        return row.id


async def log_ids(client: AsyncClient, admin: Identity, **params: Any) -> list[int]:
    response = await client.get(LOGS, params=params, headers=admin.headers)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


@pytest.mark.parametrize("who", ["user_a", "viewer"])
async def test_every_admin_route_is_403_for_other_roles_and_401_without_a_token(
    client: AsyncClient, identities: dict[str, Identity], who: str
) -> None:
    headers = identities[who].headers
    target = identities["user_b"].id
    calls = [
        ("GET", f"{ADMIN}/users", None),
        ("PATCH", f"{ADMIN}/users/{target}", {"role": "ADMIN"}),
        ("GET", LOGS, None),
        ("GET", f"{LOGS}/event-types", None),
        ("GET", f"{ADMIN}/system", None),
    ]

    for method, url, body in calls:
        denied = await client.request(method, url, json=body, headers=headers)
        anonymous = await client.request(method, url, json=body)

        assert (denied.status_code, denied.json()["error"]["code"]) == (403, "FORBIDDEN"), url
        assert anonymous.status_code == 401, url
    assert (await client.get("/api/v1/auth/me", headers=identities["user_b"].headers)).json()[
        "role"
    ] == "USER", "the refused PATCH changed nothing"


async def test_logs_are_newest_first_with_id_breaking_ties_across_pages(
    client: AsyncClient, admin: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    oldest = await add_log(session_factory, T0)
    tie_first = await add_log(session_factory, T0 + timedelta(seconds=1))
    tie_second = await add_log(session_factory, T0 + timedelta(seconds=1))
    newest = await add_log(session_factory, T0 + timedelta(seconds=2))

    pages = [await log_ids(client, admin, page=n, page_size=3, **WINDOW) for n in (1, 2)]
    total = (await client.get(LOGS, params=WINDOW, headers=admin.headers)).json()["total"]

    assert pages == [[newest, tie_second, tie_first], [oldest]]
    assert total == 4


async def test_logs_filter_by_level_event_type_and_a_half_open_window(
    client: AsyncClient, admin: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    at_start = await add_log(session_factory, T0, AuditEvent.IMPORT_FAILED, "WARNING")
    inside = await add_log(
        session_factory, T0 + timedelta(minutes=5), AuditEvent.WRITER_FAILED, "ERROR"
    )
    at_end = await add_log(
        session_factory, T0 + timedelta(hours=1), AuditEvent.IMPORT_FAILED, "WARNING"
    )

    assert await log_ids(client, admin, **WINDOW) == [inside, at_start]
    assert await log_ids(client, admin, level="WARNING", **WINDOW) == [at_start]
    assert await log_ids(client, admin, event_type="writer.failed") == [inside]
    assert await log_ids(client, admin, event_type="import.failed") == [at_end, at_start]


async def test_real_admin_actions_are_filterable_by_their_real_event_type(
    client: AsyncClient, admin: Identity, viewer: Identity, services: dict[str, int]
) -> None:
    await client.patch(f"{ADMIN}/users/{viewer.id}", json={"role": "USER"}, headers=admin.headers)
    await client.patch(
        f"/api/v1/services/{services['auth']}", json={"alert_threshold": 90}, headers=admin.headers
    )
    await client.post(
        "/api/v1/auth/token", data={"username": viewer.email, "password": "nope-nope"}
    )

    async def rows(**params: str) -> list[dict[str, Any]]:
        response = await client.get(LOGS, params=params, headers=admin.headers)
        return response.json()["items"]

    [user_change] = await rows(event_type="user.updated")
    [threshold_change] = await rows(event_type="service.threshold_updated")
    [failed_login] = await rows(event_type="auth.login", level="WARNING")
    assert (user_change["actor_id"], user_change["target_id"]) == (admin.id, str(viewer.id))
    assert user_change["context"] == {"before": {"role": "VIEWER"}, "after": {"role": "USER"}}
    assert threshold_change["context"]["after"] == {"alert_threshold": 90}
    assert failed_login["context"]["result"] == "failure"


async def test_event_types_list_every_name_the_application_writes(
    client: AsyncClient, admin: Identity
) -> None:
    response = await client.get(f"{LOGS}/event-types", headers=admin.headers)

    assert response.status_code == 200
    assert sorted(response.json()) == [
        "auth.login",
        "auth.register",
        "import.failed",
        "import.succeeded",
        "record.deleted",
        "record.updated",
        "service.threshold_updated",
        "user.updated",
        "writer.failed",
        "writer.recovered",
    ]


def test_an_event_type_outside_audit_event_is_refused_before_it_is_added() -> None:
    session = AsyncSession()

    with pytest.raises(ValueError, match="auth.logout"):
        audit.record_event(session, "auth.logout", "not a known event")  # type: ignore[arg-type]

    assert not session.new


@pytest.mark.parametrize(
    "params",
    [
        {"event_type": "auth.logout"},
        {"level": "DEBUG"},
        {"start": "2026-10-31T09:00:00Z", "end": "2026-10-31T08:00:00Z"},
        {"start": "2026-10-31T08:00:00"},
        {"page_size": 101},
    ],
)
async def test_log_filters_outside_the_contract_are_422(
    client: AsyncClient, admin: Identity, params: dict[str, object]
) -> None:
    response = await client.get(LOGS, params=params, headers=admin.headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_system_status_shows_readiness_pool_simulator_and_table_counts(
    client: AsyncClient,
    admin: Identity,
    pipeline: Pipeline,
    services: dict[str, int],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(2):
        await insert_record(session_factory, services["auth"], admin.id)
    await pipeline.generator.tick()

    system = await client.get(f"{ADMIN}/system", headers=admin.headers)
    ready = await client.get("/health/ready")

    assert system.status_code == 200
    body = system.json()
    readiness = body["readiness"]
    assert readiness["total_uncommitted"] == 3, "the tick's three events, not yet committed"
    assert readiness["state"] == "HEALTHY"
    without_age = {k: v for k, v in readiness.items() if k != "oldest_pending_age_s"}
    assert without_age == {k: v for k, v in ready.json().items() if k != "oldest_pending_age_s"}
    assert body["pool"]["status"].startswith("Pool size: 5 ")
    assert (body["pool"]["pool_size"], body["pool"]["max_overflow"]) == (5, 10)
    generator = body["generator"]
    assert generator["thresholds"] == {"auth": 80, "orders": 150, "payment": 80}
    assert generator["thresholds_loaded_at"] is not None
    assert (generator["paused_s"], generator["stopped_by"]) == (0.0, None)
    assert body["table_counts"] == {
        "users": 1,
        "api_services": 3,
        "import_batches": 0,
        "traffic_records": 2,
        "system_logs": 1,
    }


async def test_system_status_shows_why_the_simulator_stopped(
    client: AsyncClient, admin: Identity, pipeline: Pipeline
) -> None:
    pipeline.generator.paused_s = 12.5
    pipeline.generator.stopped_by = WriterState.UNAVAILABLE

    generator = (await client.get(f"{ADMIN}/system", headers=admin.headers)).json()["generator"]

    assert (generator["paused_s"], generator["stopped_by"]) == (12.5, "UNAVAILABLE")


async def test_system_status_is_503_while_the_database_is_down(
    client: AsyncClient, admin: Identity, pipeline: Pipeline
) -> None:
    unreachable = make_engine("mysql+asyncmy://traffic:wrong@127.0.0.1:1/traffic_test")

    async def unreachable_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(unreachable) as session:
            yield session

    app.dependency_overrides[get_session] = unreachable_session
    response = await client.get(f"{ADMIN}/system", headers=admin.headers)
    await unreachable.dispose()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


async def test_table_counts_are_reused_for_ten_seconds(
    admin: Identity,
    pipeline: Pipeline,
    services: dict[str, int],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def traffic_rows(at: float) -> int:
        async with session_factory() as session:
            status = await admin_service.system_status(session, pipeline, clock=lambda: at)
        return status.table_counts["traffic_records"]

    first = await traffic_rows(100.0)
    await insert_record(session_factory, services["auth"], admin.id)

    assert (await traffic_rows(109.9), await traffic_rows(110.0)) == (first, first + 1)
