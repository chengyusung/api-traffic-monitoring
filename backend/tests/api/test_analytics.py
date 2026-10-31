import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models import RecordSource
from app.services import analytics as analytics_service
from tests.api.test_records import insert_record
from tests.conftest import Identity

ANALYTICS = "/api/v1/analytics"
T0 = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
# (service, value, seconds after T0, source); thresholds: auth 80, orders 150, payment 80.
DATASET = [
    ("auth", 50, 0, RecordSource.MANUAL),
    ("auth", 90, 30, RecordSource.SIMULATOR),
    ("payment", 81, 10, RecordSource.SIMULATOR),
    ("auth", 70, 60, RecordSource.MANUAL),
    ("payment", 79, 70, RecordSource.SIMULATOR),
    ("orders", 120, 3600, RecordSource.IMPORT),
]
THRESHOLDS = {"auth": 80, "orders": 150, "payment": 80}


@pytest.fixture
async def dataset(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    for slug, value, seconds, source in DATASET:
        await insert_record(
            session_factory,
            services[slug],
            None,
            value=value,
            observed_at=T0 + timedelta(seconds=seconds),
            source=source,
            threshold=THRESHOLDS[slug],
        )


def metrics(count: int, total: int, low: int, high: int, anomalies: int) -> dict[str, object]:
    return {
        "count": count,
        "sum": total,
        "avg_per_observation": pytest.approx(total / count),
        "min": low,
        "max": high,
        "anomaly_count": anomalies,
    }


async def get(client: AsyncClient, viewer: Identity, path: str, **params: object) -> Any:
    response = await client.get(f"{ANALYTICS}/{path}", params=params, headers=viewer.headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.usefixtures("dataset")
async def test_summary_and_categories_match_hand_computed_values(
    client: AsyncClient, viewer: Identity
) -> None:
    assert await get(client, viewer, "summary") == metrics(6, 490, 50, 120, 2)
    assert await get(client, viewer, "categories") == [
        {"service": "auth", **metrics(3, 210, 50, 90, 1)},
        {"service": "orders", **metrics(1, 120, 120, 120, 0)},
        {"service": "payment", **metrics(2, 160, 79, 81, 1)},
    ]


@pytest.mark.usefixtures("dataset")
async def test_filters_are_the_records_list_filters(client: AsyncClient, viewer: Identity) -> None:
    for params in (
        {"service": "payment"},
        {"source": "SIMULATOR", "is_anomaly": "true"},
        {
            "start": (T0 + timedelta(seconds=30)).isoformat(),
            "end": (T0 + timedelta(hours=1)).isoformat(),
        },
    ):
        listed = await client.get("/api/v1/records", params=params, headers=viewer.headers)
        summary = await get(client, viewer, "summary", **params)
        assert summary["count"] == listed.json()["total"], params
    assert await get(client, viewer, "summary", service="payment") == metrics(2, 160, 79, 81, 1)


@pytest.mark.usefixtures("dataset")
async def test_trends_bucket_by_minute_and_hour_without_zero_fill(
    client: AsyncClient, viewer: Identity
) -> None:
    def point(minute: int, anomalies: int, *values: int) -> dict[str, object]:
        return {
            "bucket_start": (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
            **metrics(len(values), sum(values), min(values), max(values), anomalies),
        }

    assert await get(client, viewer, "trends", bucket="minute") == [
        point(0, 2, 50, 90, 81),
        point(1, 0, 70, 79),
        point(60, 0, 120),
    ]
    assert await get(client, viewer, "trends", bucket="hour") == [
        point(0, 2, 50, 90, 81, 70, 79),
        point(60, 0, 120),
    ]


@pytest.mark.usefixtures("dataset")
async def test_export_is_an_xlsx_with_one_row_per_filtered_record(
    client: AsyncClient, viewer: Identity
) -> None:
    params = {"service": "auth", "start": T0.isoformat()}

    response = await client.get(f"{ANALYTICS}/export", params=params, headers=viewer.headers)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    sheet = load_workbook(io.BytesIO(response.content)).active
    assert sheet is not None
    header, *rows = sheet.iter_rows(values_only=True)
    listed = await client.get("/api/v1/records", params=params, headers=viewer.headers)
    assert len(rows) == listed.json()["total"] == 3
    by_name = [dict(zip(header, row, strict=True)) for row in rows]
    # Newest first, like the records list; Excel keeps the UTC wall clock without a zone.
    assert [(r["service"], r["value"], r["source"]) for r in by_name] == [
        ("auth", 70, "MANUAL"),
        ("auth", 90, "SIMULATOR"),
        ("auth", 50, "MANUAL"),
    ]
    assert by_name[0]["observed_at_utc"] == datetime(2026, 10, 31, 8, 1)
    assert by_name[1]["is_anomaly"] is True


@pytest.mark.usefixtures("dataset")
async def test_more_trend_buckets_than_the_cap_is_rejected(
    client: AsyncClient, viewer: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analytics_service, "TREND_MAX_BUCKETS", 2)

    minute, hour = [
        await client.get(f"{ANALYTICS}/trends", params={"bucket": b}, headers=viewer.headers)
        for b in ("minute", "hour")
    ]

    # The dataset spans three minute buckets and two hour buckets.
    assert (minute.status_code, hour.status_code, len(hour.json())) == (422, 200, 2)
    assert minute.json()["error"]["code"] == "VALIDATION_ERROR"


async def export_titles(client: AsyncClient, viewer: Identity) -> dict[str, str]:
    """Title -> openpyxl data type of its cell in the exported sheet."""
    response = await client.get(f"{ANALYTICS}/export", headers=viewer.headers)
    assert response.status_code == 200, response.text
    sheet = load_workbook(io.BytesIO(response.content)).active
    assert sheet is not None
    header, *rows = sheet.iter_rows()
    column = [cell.value for cell in header].index("title")
    return {row[column].value: row[column].data_type for row in rows}


async def test_an_exported_title_is_a_text_cell_even_when_it_looks_like_a_formula(
    client: AsyncClient,
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    services: dict[str, int],
) -> None:
    formula = '=HYPERLINK("https://example.com","open")'
    await insert_record(session_factory, services["auth"], None, title=formula)

    assert await export_titles(client, viewer) == {formula: "s"}


async def test_a_stored_control_character_is_dropped_instead_of_failing_the_export(
    client: AsyncClient,
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    services: dict[str, int],
) -> None:
    # A row written before titles were validated, or by a path that skips the schema.
    await insert_record(session_factory, services["auth"], None, title="tab\x0bbed")

    assert await export_titles(client, viewer) == {"tabbed": "s"}


@pytest.mark.usefixtures("dataset")
async def test_export_over_the_row_limit_is_rejected(
    client: AsyncClient, viewer: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "export_max_rows", len(DATASET) - 1)

    response = await client.get(f"{ANALYTICS}/export", headers=viewer.headers)
    narrowed = await client.get(
        f"{ANALYTICS}/export", params={"service": "auth"}, headers=viewer.headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert narrowed.status_code == 200


async def test_a_bad_filter_or_bucket_is_a_validation_error(
    client: AsyncClient, viewer: Identity
) -> None:
    for path, params in (
        ("summary", {"start": T0.isoformat(), "end": (T0 - timedelta(seconds=1)).isoformat()}),
        ("summary", {"page_size": 5}),
        ("trends", {"bucket": "day"}),
        ("categories", {"start": "2026-10-31T08:00:00"}),
    ):
        response = await client.get(f"{ANALYTICS}/{path}", params=params, headers=viewer.headers)
        assert response.status_code == 422, (path, params)
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_viewer_reads_analytics_but_cannot_import(
    client: AsyncClient, identities: dict[str, Identity]
) -> None:
    viewer = identities["viewer"]
    for path in ("summary", "categories", "trends", "export"):
        response = await client.get(f"{ANALYTICS}/{path}", headers=viewer.headers)
        assert response.status_code == 200, path

    response = await client.post(
        "/api/v1/imports",
        files={"file": ("x.csv", b"event_id,service_slug,title,value,observed_at\n")},
        headers=viewer.headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_analytics_requires_a_token(client: AsyncClient) -> None:
    response = await client.get(f"{ANALYTICS}/summary")

    assert response.status_code == 401
