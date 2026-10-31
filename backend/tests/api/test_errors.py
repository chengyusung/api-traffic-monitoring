from collections.abc import AsyncIterator, Callable

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import exc

from app.core.errors import NotFoundError
from app.main import create_app

DB_ERRORS: dict[str, tuple[Callable[[], Exception], int]] = {
    "OperationalError": (lambda: exc.OperationalError("SELECT 1", None, Exception("lost")), 503),
    "InterfaceError": (lambda: exc.InterfaceError("SELECT 1", None, Exception("closed")), 503),
    "pool TimeoutError": (lambda: exc.TimeoutError("QueuePool limit reached"), 503),
    "IntegrityError": (lambda: exc.IntegrityError("INSERT", None, Exception("1452")), 500),
    "DataError": (lambda: exc.DataError("INSERT", None, Exception("1406")), 500),
}


@pytest.fixture
async def probe() -> AsyncIterator[AsyncClient]:
    app = create_app()

    @app.get("/probe/missing")
    async def missing() -> None:
        raise NotFoundError("Record 42 does not exist.")

    @app.get("/probe/items")
    async def items(limit: int) -> None:
        return None

    @app.get("/probe/db/{name}")
    async def database_error(name: str) -> None:
        raise DB_ERRORS[name][0]()

    @app.get("/probe/crash")
    async def crash() -> None:
        raise RuntimeError("password=hunter2")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def envelope(response_json: dict[str, dict[str, str]]) -> dict[str, str]:
    assert response_json.keys() == {"error"}
    return response_json["error"]


async def test_app_error_uses_its_status_and_code(probe: AsyncClient) -> None:
    response = await probe.get("/probe/missing")

    assert response.status_code == 404
    assert envelope(response.json()) == {
        "code": "NOT_FOUND",
        "message": "Record 42 does not exist.",
        "request_id": response.headers["X-Request-ID"],
    }


async def test_validation_error_is_422_with_field_path(probe: AsyncClient) -> None:
    response = await probe.get("/probe/items", params={"limit": "many"})

    assert response.status_code == 422
    error = envelope(response.json())
    assert error["code"] == "VALIDATION_ERROR"
    assert "query.limit" in error["message"]
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_unknown_route_is_404_envelope(probe: AsyncClient) -> None:
    response = await probe.get("/nope")

    assert response.status_code == 404
    assert envelope(response.json())["code"] == "NOT_FOUND"


async def test_unhandled_exception_is_500_without_internal_details(probe: AsyncClient) -> None:
    response = await probe.get("/probe/crash")

    assert response.status_code == 500
    error = envelope(response.json())
    assert error["code"] == "INTERNAL_SERVER_ERROR"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert "hunter2" not in response.text


@pytest.mark.parametrize("name", DB_ERRORS)
async def test_only_connection_level_database_errors_are_503(probe: AsyncClient, name: str) -> None:
    status = DB_ERRORS[name][1]

    response = await probe.get(f"/probe/db/{name}")

    assert response.status_code == status
    error = envelope(response.json())
    assert error["code"] == {503: "SERVICE_UNAVAILABLE", 500: "INTERNAL_SERVER_ERROR"}[status]
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert "SELECT" not in response.text and "INSERT" not in response.text


async def test_valid_incoming_request_id_is_echoed(probe: AsyncClient) -> None:
    response = await probe.get("/nope", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"
    assert envelope(response.json())["request_id"] == "abc-123"


async def test_malformed_incoming_request_id_is_replaced(probe: AsyncClient) -> None:
    response = await probe.get("/nope", headers={"X-Request-ID": "x" * 65})

    assert response.headers["X-Request-ID"] != "x" * 65
    assert envelope(response.json())["request_id"] == response.headers["X-Request-ID"]
