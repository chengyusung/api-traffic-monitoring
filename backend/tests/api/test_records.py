import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.records import BULK_MAX_ITEMS
from app.models import ApiService, RecordSource, SystemLog, TrafficRecord
from tests.conftest import Identity

RECORDS = "/api/v1/records"
T0 = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)


def new_record(**overrides: Any) -> dict[str, Any]:
    return {
        "service_slug": "payment",
        "title": "checkout",
        "value": 50,
        "observed_at": T0.isoformat(),
        **overrides,
    }


async def record_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(TrafficRecord)) or 0


async def insert_record(
    session_factory: async_sessionmaker[AsyncSession],
    service_id: int,
    created_by: int | None,
    *,
    value: int = 50,
    title: str = "seeded",
    observed_at: datetime = T0,
    source: RecordSource = RecordSource.MANUAL,
) -> int:
    """A row as the simulator or another writer would leave it (created_by may be NULL)."""
    async with session_factory() as session:
        record = TrafficRecord(
            event_id=str(uuid.uuid4()),
            service_id=service_id,
            title=title,
            value=value,
            source=source,
            created_by=created_by,
            observed_at=observed_at,
            persisted_at=datetime.now(UTC),
            threshold_applied=80,
            is_anomaly=value > 80,
        )
        session.add(record)
        await session.commit()
        return record.id


@pytest.mark.parametrize("who", ["admin", "user_a"])
async def test_writer_creates_a_manual_record_owned_by_them(
    client: AsyncClient, services: dict[str, int], identities: dict[str, Identity], who: str
) -> None:
    identity = identities[who]

    response = await client.post(RECORDS, json=new_record(), headers=identity.headers)

    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "MANUAL"
    assert body["created_by"] == identity.id
    assert body["service_id"] == services["payment"]
    assert body["threshold_applied"] == 80
    assert uuid.UUID(body["event_id"]).version == 4
    assert body["observed_at"] == "2026-10-31T08:00:00Z"


@pytest.mark.parametrize(("value", "anomaly"), [(79, False), (80, False), (81, True)])
async def test_threshold_boundary_through_the_api(
    client: AsyncClient, services: dict[str, int], user_a: Identity, value: int, anomaly: bool
) -> None:
    response = await client.post(RECORDS, json=new_record(value=value), headers=user_a.headers)

    assert response.json()["is_anomaly"] is anomaly


async def test_client_event_id_is_kept_and_a_duplicate_is_409(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event_id = str(uuid.uuid4())

    first = await client.post(RECORDS, json=new_record(event_id=event_id), headers=user_a.headers)
    second = await client.post(RECORDS, json=new_record(event_id=event_id), headers=user_a.headers)

    assert first.json()["event_id"] == event_id
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    assert await record_count(session_factory) == 1


FORBIDDEN_FIELDS = {
    "source": "IMPORT",
    "created_by": 1,
    "is_anomaly": False,
    "threshold_applied": 1000,
    "persisted_at": "2026-10-31T00:00:00Z",
    "import_batch_id": 1,
}


@pytest.mark.parametrize("field", FORBIDDEN_FIELDS)
async def test_server_owned_fields_are_422_on_create_and_update(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    field: str,
) -> None:
    record_id = await insert_record(session_factory, services["payment"], user_a.id)
    smuggled = {field: FORBIDDEN_FIELDS[field]}

    create = await client.post(RECORDS, json=new_record(**smuggled), headers=user_a.headers)
    update = await client.patch(f"{RECORDS}/{record_id}", json=smuggled, headers=user_a.headers)

    for response in (create, update):
        assert response.status_code == 422
        assert field in response.json()["error"]["message"]
    assert await record_count(session_factory) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {"value": -1},
        {"value": "5"},
        {"observed_at": "2026-10-31T08:00:00"},
        {"observed_at": "0001-01-01T00:00:00+01:00"},
        {"service_slug": "unknown"},
        {"title": ""},
    ],
    ids=[
        "negative",
        "string value",
        "naive datetime",
        "no UTC equivalent",
        "unknown service",
        "empty title",
    ],
)
async def test_invalid_record_is_422(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    invalid: dict[str, Any],
) -> None:
    response = await client.post(RECORDS, json=new_record(**invalid), headers=user_a.headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await record_count(session_factory) == 0


async def test_an_oversized_service_slug_is_not_echoed(
    client: AsyncClient, services: dict[str, int], user_a: Identity
) -> None:
    response = await client.post(
        RECORDS, json=new_record(service_slug="p" * 33), headers=user_a.headers
    )

    assert response.status_code == 422
    assert "p" * 33 not in response.text


async def test_an_inactive_service_is_unknown_for_new_records(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        payment = await session.get(ApiService, services["payment"])
        assert payment is not None
        payment.is_active = False
        await session.commit()

    response = await client.post(RECORDS, json=new_record(), headers=user_a.headers)

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Unknown service_slug: payment"
    assert await record_count(session_factory) == 0


async def test_viewer_cannot_write_through_any_endpoint(
    client: AsyncClient,
    services: dict[str, int],
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record_id = await insert_record(session_factory, services["payment"], viewer.id)

    responses = [
        await client.post(RECORDS, json=new_record(), headers=viewer.headers),
        await client.post(f"{RECORDS}/bulk", json=[new_record()], headers=viewer.headers),
        await client.patch(f"{RECORDS}/{record_id}", json={"value": 1}, headers=viewer.headers),
        await client.delete(f"{RECORDS}/{record_id}", headers=viewer.headers),
    ]

    assert [r.status_code for r in responses] == [403, 403, 403, 403]
    assert {r.json()["error"]["code"] for r in responses} == {"FORBIDDEN"}
    assert await record_count(session_factory) == 1
    async with session_factory() as session:
        record = await session.get(TrafficRecord, record_id)
    assert record is not None
    assert record.value == 50


async def test_every_role_reads_every_record(
    client: AsyncClient,
    services: dict[str, int],
    admin: Identity,
    user_a: Identity,
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record_id = await insert_record(session_factory, services["auth"], None)

    for identity in (admin, user_a, viewer):
        one = await client.get(f"{RECORDS}/{record_id}", headers=identity.headers)
        page = await client.get(RECORDS, headers=identity.headers)

        assert one.status_code == 200
        assert [item["id"] for item in page.json()["items"]] == [record_id]


async def test_reading_without_a_token_is_401(client: AsyncClient) -> None:
    response = await client.get(RECORDS)

    assert response.status_code == 401


# ADR-002 role matrix, rows "own record" and "other's or simulator's record", for writers.
@pytest.mark.parametrize(
    ("actor", "owner", "allowed"),
    [
        ("user_a", "user_a", True),
        ("user_a", "user_b", False),
        ("user_a", None, False),
        ("user_a", "admin", False),
        ("admin", "admin", True),
        ("admin", "user_b", True),
        ("admin", None, True),
    ],
)
async def test_patch_and_delete_follow_ownership(
    client: AsyncClient,
    services: dict[str, int],
    session_factory: async_sessionmaker[AsyncSession],
    identities: dict[str, Identity],
    actor: str,
    owner: str | None,
    allowed: bool,
) -> None:
    identity = identities[actor]
    owner_id = identities[owner].id if owner else None
    record_id = await insert_record(session_factory, services["payment"], owner_id)

    patch = await client.patch(
        f"{RECORDS}/{record_id}", json={"title": "edited"}, headers=identity.headers
    )
    delete = await client.delete(f"{RECORDS}/{record_id}", headers=identity.headers)

    if allowed:
        assert (patch.status_code, delete.status_code) == (200, 204)
        assert await record_count(session_factory) == 0
    else:
        assert (patch.status_code, delete.status_code) == (404, 404)
        assert await record_count(session_factory) == 1


async def test_forbidden_and_missing_records_look_the_same(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    user_b: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    others = await insert_record(session_factory, services["payment"], user_b.id)

    forbidden = await client.delete(f"{RECORDS}/{others}", headers=user_a.headers)
    missing = await client.delete(f"{RECORDS}/{others + 1000}", headers=user_a.headers)

    assert forbidden.status_code == missing.status_code == 404
    seen, absent = forbidden.json()["error"], missing.json()["error"]
    assert (seen["code"], seen["message"]) == (absent["code"], absent["message"])


async def test_value_edit_recomputes_with_the_records_own_threshold(
    client: AsyncClient,
    services: dict[str, int],
    admin: Identity,
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await client.post(RECORDS, json=new_record(value=70), headers=user_a.headers)
    record_id = created.json()["id"]
    await client.patch(
        f"/api/v1/services/{services['payment']}",
        json={"alert_threshold": 50},
        headers=admin.headers,
    )

    still_normal = await client.patch(
        f"{RECORDS}/{record_id}", json={"value": 75}, headers=user_a.headers
    )
    anomaly = await client.patch(
        f"{RECORDS}/{record_id}", json={"value": 81}, headers=user_a.headers
    )

    assert still_normal.json()["threshold_applied"] == 80
    assert still_normal.json()["is_anomaly"] is False
    assert anomaly.json()["is_anomaly"] is True
    async with session_factory() as session:
        logs = await session.scalars(
            select(SystemLog).where(SystemLog.event_type == "record.updated").order_by(SystemLog.id)
        )
        contexts = [log.context for log in logs]
    assert contexts[-1] == {
        "before": {"value": 75, "is_anomaly": False},
        "after": {"value": 81, "is_anomaly": True},
    }


async def update_audits(session_factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(SystemLog.context)
            .where(SystemLog.event_type == "record.updated")
            .order_by(SystemLog.id)
        )
        return list(rows)


async def test_patch_with_an_empty_body_is_422(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record_id = await insert_record(session_factory, services["payment"], user_a.id)

    response = await client.patch(f"{RECORDS}/{record_id}", json={}, headers=user_a.headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_patch_audits_each_changed_field_with_before_and_after(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record_id = await insert_record(session_factory, services["payment"], user_a.id, title="old")
    changes = {"title": "new", "value": 50, "observed_at": "2026-10-31T16:05:00+08:00"}

    response = await client.patch(f"{RECORDS}/{record_id}", json=changes, headers=user_a.headers)

    assert response.status_code == 200
    assert await update_audits(session_factory) == [
        {
            "before": {"title": "old", "observed_at": "2026-10-31T08:00:00Z"},
            "after": {"title": "new", "observed_at": "2026-10-31T08:05:00Z"},
        }
    ]


async def test_patch_that_changes_nothing_writes_no_audit_row(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record_id = await insert_record(session_factory, services["payment"], user_a.id)
    same = {"title": "seeded", "value": 50, "observed_at": T0.isoformat()}

    response = await client.patch(f"{RECORDS}/{record_id}", json=same, headers=user_a.headers)

    assert response.status_code == 200
    assert await update_audits(session_factory) == []


async def test_delete_is_audited(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record_id = await insert_record(session_factory, services["payment"], user_a.id)

    response = await client.delete(f"{RECORDS}/{record_id}", headers=user_a.headers)

    assert response.status_code == 204
    assert (await client.get(f"{RECORDS}/{record_id}", headers=user_a.headers)).status_code == 404
    async with session_factory() as session:
        log = await session.scalar(
            select(SystemLog).where(SystemLog.event_type == "record.deleted")
        )
    assert log is not None
    assert (log.actor_id, log.target_id) == (user_a.id, str(record_id))
    assert log.request_id == response.headers["X-Request-ID"]


async def test_bulk_creates_every_item_with_the_same_rules(
    client: AsyncClient, services: dict[str, int], user_a: Identity
) -> None:
    items = [new_record(value=81), new_record(service_slug="orders", value=81)]

    response = await client.post(f"{RECORDS}/bulk", json=items, headers=user_a.headers)

    assert response.status_code == 201
    assert [(r["threshold_applied"], r["is_anomaly"]) for r in response.json()] == [
        (80, True),
        (150, False),
    ]
    assert {r["created_by"] for r in response.json()} == {user_a.id}


@pytest.mark.parametrize(
    ("bad_tail", "status"),
    [
        ([new_record(value=-1)], 422),
        ([new_record(service_slug="unknown")], 422),
        ([new_record(event_id="00000000-0000-4000-8000-000000000001")] * 2, 409),
    ],
    ids=["invalid item", "unknown service", "duplicate event_id in batch"],
)
async def test_bulk_is_all_or_nothing(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    bad_tail: list[dict[str, Any]],
    status: int,
) -> None:
    items = [new_record(), new_record(), *bad_tail]

    response = await client.post(f"{RECORDS}/bulk", json=items, headers=user_a.headers)

    assert response.status_code == status
    assert await record_count(session_factory) == 0


async def test_bulk_conflicting_with_an_existing_event_id_writes_nothing(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    existing = await client.post(RECORDS, json=new_record(), headers=user_a.headers)
    clash = new_record(event_id=existing.json()["event_id"])

    response = await client.post(
        f"{RECORDS}/bulk", json=[new_record(), clash], headers=user_a.headers
    )

    assert response.status_code == 409
    assert await record_count(session_factory) == 1


async def test_bulk_accepts_up_to_the_limit_and_rejects_one_more(
    client: AsyncClient,
    services: dict[str, int],
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bulk = f"{RECORDS}/bulk"

    over = await client.post(
        bulk, json=[new_record()] * (BULK_MAX_ITEMS + 1), headers=user_a.headers
    )
    at_limit = await client.post(bulk, json=[new_record()] * BULK_MAX_ITEMS, headers=user_a.headers)

    assert (over.status_code, at_limit.status_code) == (422, 201)
    assert await record_count(session_factory) == BULK_MAX_ITEMS


async def test_bulk_rejects_an_empty_list(
    client: AsyncClient, services: dict[str, int], user_a: Identity
) -> None:
    response = await client.post(f"{RECORDS}/bulk", json=[], headers=user_a.headers)

    assert response.status_code == 422


async def list_ids(client: AsyncClient, identity: Identity, **params: Any) -> list[int]:
    response = await client.get(RECORDS, params=params, headers=identity.headers)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def test_pagination_total_and_pages_cover_every_row_once(
    client: AsyncClient,
    services: dict[str, int],
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [
        await insert_record(
            session_factory, services["auth"], None, observed_at=T0 + timedelta(seconds=i)
        )
        for i in range(25)
    ]

    first = await client.get(RECORDS, params={"page_size": 10}, headers=viewer.headers)
    pages = [await list_ids(client, viewer, page=page, page_size=10) for page in (1, 2, 3, 4)]

    assert {k: v for k, v in first.json().items() if k != "items"} == {
        "page": 1,
        "page_size": 10,
        "total": 25,
    }
    assert [len(page) for page in pages] == [10, 10, 5, 0]
    # Default sort is -observed_at, so newest first.
    assert [i for page in pages for i in page] == ids[::-1]


async def test_default_page_size_is_20(
    client: AsyncClient,
    services: dict[str, int],
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(21):
        await insert_record(session_factory, services["auth"], None)

    response = await client.get(RECORDS, headers=viewer.headers)

    assert (len(response.json()["items"]), response.json()["total"]) == (20, 21)


@pytest.mark.parametrize(
    "sort", ["observed_at", "-observed_at", "value", "-value", "persisted_at", "-persisted_at"]
)
async def test_sort_breaks_ties_by_id_so_pages_are_stable(
    client: AsyncClient,
    services: dict[str, int],
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    sort: str,
) -> None:
    # Rows tie on value and observed_at; persisted_at follows insert order.
    ids = [await insert_record(session_factory, services["auth"], None) for _ in range(7)]
    expected = ids[::-1] if sort.startswith("-") else ids

    pages = [await list_ids(client, viewer, page=p, page_size=3, sort=sort) for p in (1, 2, 3)]

    assert [i for page in pages for i in page] == expected


async def test_sort_by_value_descending(
    client: AsyncClient,
    services: dict[str, int],
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    by_value = {
        value: await insert_record(session_factory, services["auth"], None, value=value)
        for value in (5, 90, 40)
    }

    assert await list_ids(client, viewer, sort="-value") == [
        by_value[90],
        by_value[40],
        by_value[5],
    ]
    assert await list_ids(client, viewer, sort="value") == [by_value[5], by_value[40], by_value[90]]


@pytest.mark.parametrize(
    "params",
    [
        {"page_size": 101},
        {"page_size": 0},
        {"page": 0},
        {"page": 10001},
        {"sort": "title"},
        {"sort": "created_at"},
        {"bogus": "1"},
        {"start": "2026-11-01T00:00:00Z", "end": "2026-10-31T00:00:00Z"},
        {"end": "9999-12-31T23:59:59-01:00"},
    ],
)
async def test_list_params_outside_the_contract_are_422(
    client: AsyncClient, viewer: Identity, params: dict[str, Any]
) -> None:
    response = await client.get(RECORDS, params=params, headers=viewer.headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_bounds_are_inclusive(client: AsyncClient, viewer: Identity) -> None:
    assert await list_ids(client, viewer, page=10000) == []
    assert await list_ids(client, viewer, start=T0.isoformat(), end=T0.isoformat()) == []


async def test_filters(
    client: AsyncClient,
    services: dict[str, int],
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def add(slug: str, **kwargs: Any) -> int:
        return await insert_record(session_factory, services[slug], None, **kwargs)

    at_start = await add("payment", title="cart 100% full", observed_at=T0)
    anomaly = await add("payment", value=81, observed_at=T0 + timedelta(seconds=1))
    imported = await add("orders", source=RecordSource.IMPORT, observed_at=T0)
    at_end = await add("auth", title="login_burst", observed_at=T0 + timedelta(seconds=2))

    async def ids(**params: Any) -> set[int]:
        return set(await list_ids(client, viewer, **params))

    assert await ids(service="payment") == {at_start, anomaly}
    assert await ids(service="nope") == set()
    assert await ids(source="IMPORT") == {imported}
    assert await ids(is_anomaly="true") == {anomaly}
    window = {"start": T0.isoformat(), "end": (T0 + timedelta(seconds=2)).isoformat()}
    assert await ids(**window) == {at_start, anomaly, imported}
    assert await ids(q="100%") == {at_start}
    assert await ids(q="_burst") == {at_end}
    assert await ids(q="%") == {at_start}
