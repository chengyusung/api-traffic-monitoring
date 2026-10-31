import json
from pathlib import Path
from typing import Any

from httpx import AsyncClient

from tests.conftest import Identity

IMPORTS = "/api/v1/imports"
DATA = Path(__file__).resolve().parents[3] / "data"


async def upload(client: AsyncClient, who: Identity, name: str, content: bytes) -> dict[str, Any]:
    response = await client.post(IMPORTS, files={"file": (name, content)}, headers=who.headers)
    return {"status": response.status_code, "body": response.json(), "headers": response.headers}


async def test_a_writer_imports_the_sample_json_and_reads_the_batch_back(
    client: AsyncClient, identities: dict[str, Identity], services: dict[str, int]
) -> None:
    user = identities["user_a"]
    content = (DATA / "sample_traffic.json").read_bytes()

    created = await upload(client, user, "sample_traffic.json", content)

    assert created["status"] == 201, created["body"]
    batch = created["body"]
    rows = len(json.loads(content))
    assert {k: batch[k] for k in ("status", "total_count", "success_count", "failure_count")} == {
        "status": "SUCCEEDED",
        "total_count": rows,
        "success_count": rows,
        "failure_count": 0,
    }
    assert (batch["filename"], batch["uploaded_by"], batch["errors"]) == (
        "sample_traffic.json",
        user.id,
        None,
    )
    records = await client.get("/api/v1/records", params={"source": "IMPORT"}, headers=user.headers)
    assert records.json()["total"] == rows
    assert {r["import_batch_id"] for r in records.json()["items"]} == {batch["id"]}

    for who, expected in (("user_a", 200), ("admin", 200), ("user_b", 404)):
        response = await client.get(f"{IMPORTS}/{batch['id']}", headers=identities[who].headers)
        assert response.status_code == expected, who
    missing = await client.get(f"{IMPORTS}/{batch['id'] + 1}", headers=user.headers)
    assert missing.json()["error"]["code"] == "NOT_FOUND"


async def test_a_rejected_file_returns_row_errors_in_the_error_envelope(
    client: AsyncClient, identities: dict[str, Identity], services: dict[str, int]
) -> None:
    lines = (DATA / "sample_traffic.csv").read_text().splitlines()
    lines[3] = lines[3].replace(",payment,", ",billing,")

    rejected = await upload(client, identities["user_a"], "broken.csv", "\n".join(lines).encode())

    assert rejected["status"] == 422
    error = rejected["body"]["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["request_id"] == rejected["headers"]["X-Request-ID"]
    assert error["details"] == [
        {"row": 4, "field": "service_slug", "message": "unknown or inactive service"}
    ]
    assert "row 4" in error["message"]
    records = await client.get("/api/v1/records", headers=identities["user_a"].headers)
    assert records.json()["total"] == 0


async def test_an_existing_event_id_is_a_conflict_for_the_whole_file(
    client: AsyncClient, identities: dict[str, Identity], services: dict[str, int]
) -> None:
    content = (DATA / "sample_traffic.csv").read_bytes()
    first = await upload(client, identities["user_a"], "sample_traffic.csv", content)

    second = await upload(client, identities["user_b"], "sample_traffic.csv", content)

    assert (first["status"], second["status"]) == (201, 409)
    assert second["body"]["error"]["code"] == "CONFLICT"
    assert {d["row"] for d in second["body"]["error"]["details"]} == {14, 15, 16}
    total = await client.get("/api/v1/records", headers=identities["user_b"].headers)
    assert total.json()["total"] == first["body"]["total_count"]


async def test_imported_rows_belong_to_the_uploader(
    client: AsyncClient, identities: dict[str, Identity], services: dict[str, int]
) -> None:
    content = b"event_id,service_slug,title,value,observed_at\n,auth,a,1,2026-10-31T08:00:00Z\n"
    await upload(client, identities["user_a"], "one.csv", content)
    listed = await client.get("/api/v1/records", headers=identities["user_a"].headers)
    record_id = listed.json()["items"][0]["id"]

    by_other = await client.delete(
        f"/api/v1/records/{record_id}", headers=identities["user_b"].headers
    )
    by_owner = await client.delete(
        f"/api/v1/records/{record_id}", headers=identities["user_a"].headers
    )

    assert (by_other.status_code, by_owner.status_code) == (404, 204)


async def test_an_upload_without_a_file_or_a_token_is_refused(
    client: AsyncClient, identities: dict[str, Identity]
) -> None:
    no_file = await client.post(IMPORTS, headers=identities["user_a"].headers)
    no_token = await client.post(IMPORTS, files={"file": ("x.csv", b"")})

    assert (no_file.status_code, no_token.status_code) == (422, 401)
