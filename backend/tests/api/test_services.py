import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiService, SystemLog
from tests.conftest import Identity

SERVICES = "/api/v1/services"


async def test_every_role_lists_the_three_services(
    client: AsyncClient,
    services: dict[str, int],
    admin: Identity,
    user_a: Identity,
    viewer: Identity,
) -> None:
    for identity in (admin, user_a, viewer):
        response = await client.get(SERVICES, headers=identity.headers)

        assert response.status_code == 200
        assert {(s["slug"], s["alert_threshold"]) for s in response.json()} == {
            ("auth", 80),
            ("orders", 150),
            ("payment", 80),
        }


async def test_admin_patches_threshold_and_it_is_audited(
    client: AsyncClient,
    services: dict[str, int],
    admin: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.patch(
        f"{SERVICES}/{services['payment']}", json={"alert_threshold": 95}, headers=admin.headers
    )

    assert response.status_code == 200
    assert response.json()["alert_threshold"] == 95
    async with session_factory() as session:
        log = await session.scalar(
            select(SystemLog).where(SystemLog.event_type == "service.threshold_updated")
        )
    assert log is not None
    assert log.actor_id == admin.id
    assert (log.target_type, log.target_id) == ("api_service", str(services["payment"]))
    assert log.context == {"before": {"alert_threshold": 80}, "after": {"alert_threshold": 95}}
    assert log.request_id == response.headers["X-Request-ID"]


@pytest.mark.parametrize("who", ["user_a", "viewer"])
async def test_non_admin_cannot_patch_threshold(
    client: AsyncClient,
    services: dict[str, int],
    session_factory: async_sessionmaker[AsyncSession],
    identities: dict[str, Identity],
    who: str,
) -> None:
    identity = identities[who]

    response = await client.patch(
        f"{SERVICES}/{services['auth']}", json={"alert_threshold": 1}, headers=identity.headers
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    async with session_factory() as session:
        service = await session.get(ApiService, services["auth"])
    assert service is not None
    assert service.alert_threshold == 80


@pytest.mark.parametrize("threshold", [-1, 1.5, "80", None])
async def test_threshold_must_be_a_non_negative_integer(
    client: AsyncClient, services: dict[str, int], admin: Identity, threshold: object
) -> None:
    response = await client.patch(
        f"{SERVICES}/{services['auth']}", json={"alert_threshold": threshold}, headers=admin.headers
    )

    assert response.status_code == 422


async def test_patch_unknown_service_is_404(
    client: AsyncClient, services: dict[str, int], admin: Identity
) -> None:
    response = await client.patch(
        f"{SERVICES}/999999", json={"alert_threshold": 1}, headers=admin.headers
    )

    assert response.status_code == 404
