import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEvent, Role, SystemLog, User
from tests.api.test_records import RECORDS, new_record
from tests.conftest import Identity, inserted

USERS = "/api/v1/admin/users"


async def user_row(session_factory: async_sessionmaker[AsyncSession], user_id: int) -> User:
    async with session_factory() as session:
        user = await session.get(User, user_id)
    assert user is not None
    return user


async def user_audit_rows(session_factory: async_sessionmaker[AsyncSession]) -> list[SystemLog]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(SystemLog)
            .where(SystemLog.event_type == AuditEvent.USER_UPDATED)
            .order_by(SystemLog.id)
        )
        return list(rows)


def refused(response_json: dict[str, dict[str, str]], message: str) -> None:
    assert response_json["error"]["code"] == "VALIDATION_ERROR"
    assert message in response_json["error"]["message"]


async def test_admin_lists_users_a_page_at_a_time(
    client: AsyncClient, identities: dict[str, Identity]
) -> None:
    admin = identities["admin"]
    by_id = {identity.id: identity.email for identity in identities.values()}

    first = await client.get(USERS, params={"page_size": 3}, headers=admin.headers)
    second = await client.get(USERS, params={"page_size": 3, "page": 2}, headers=admin.headers)

    assert first.status_code == second.status_code == 200
    assert (first.json()["total"], first.json()["page"], first.json()["page_size"]) == (4, 1, 3)
    rows = first.json()["items"] + second.json()["items"]
    assert [row["id"] for row in rows] == sorted(by_id), "every user once, in id order"
    assert {row["id"]: row["email"] for row in rows} == by_id
    assert set(rows[0]) == {"id", "email", "role", "is_active", "created_at"}


@pytest.mark.parametrize("params", [{"page_size": 101}, {"page": 0}, {"page_size": "all"}])
async def test_user_list_params_outside_the_contract_are_422(
    client: AsyncClient, admin: Identity, params: dict[str, object]
) -> None:
    response = await client.get(USERS, params=params, headers=admin.headers)

    assert response.status_code == 422


async def test_role_change_is_audited_with_actor_target_and_both_values(
    client: AsyncClient,
    admin: Identity,
    viewer: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.patch(
        f"{USERS}/{viewer.id}", json={"role": "USER"}, headers=admin.headers
    )

    assert response.status_code == 200
    assert (response.json()["role"], response.json()["email"]) == ("USER", viewer.email)
    assert (await user_row(session_factory, viewer.id)).role == Role.USER
    [log] = await user_audit_rows(session_factory)
    assert (log.actor_id, log.target_type, log.target_id) == (admin.id, "user", str(viewer.id))
    assert log.context == {"before": {"role": "VIEWER"}, "after": {"role": "USER"}}
    assert log.request_id == response.headers["X-Request-ID"]


async def test_a_promotion_works_on_the_very_next_request_with_the_old_token(
    client: AsyncClient, admin: Identity, viewer: Identity, services: dict[str, int]
) -> None:
    before = await client.post(RECORDS, json=new_record(), headers=viewer.headers)
    await client.patch(f"{USERS}/{viewer.id}", json={"role": "USER"}, headers=admin.headers)
    after = await client.post(RECORDS, json=new_record(), headers=viewer.headers)

    assert (before.status_code, after.status_code) == (403, 201)


async def test_a_demotion_works_on_the_very_next_request_with_the_old_token(
    client: AsyncClient, admin: Identity, user_a: Identity, services: dict[str, int]
) -> None:
    before = await client.post(RECORDS, json=new_record(), headers=user_a.headers)
    await client.patch(f"{USERS}/{user_a.id}", json={"role": "VIEWER"}, headers=admin.headers)
    after = await client.post(RECORDS, json=new_record(), headers=user_a.headers)

    assert (before.status_code, after.status_code) == (201, 403)


async def test_deactivation_locks_out_the_old_token_and_reactivation_restores_it(
    client: AsyncClient, admin: Identity, user_a: Identity
) -> None:
    off = await client.patch(
        f"{USERS}/{user_a.id}", json={"is_active": False}, headers=admin.headers
    )
    locked_out = await client.get("/api/v1/auth/me", headers=user_a.headers)
    on = await client.patch(f"{USERS}/{user_a.id}", json={"is_active": True}, headers=admin.headers)
    back = await client.get("/api/v1/auth/me", headers=user_a.headers)

    assert (off.status_code, off.json()["is_active"]) == (200, False)
    assert locked_out.status_code == 401
    assert (on.status_code, back.status_code) == (200, 200)


@pytest.mark.parametrize(
    "changes", [{"is_active": False}, {"role": "USER"}, {"role": "VIEWER", "is_active": False}]
)
async def test_an_admin_cannot_deactivate_or_demote_themself(
    client: AsyncClient,
    identities: dict[str, Identity],
    session_factory: async_sessionmaker[AsyncSession],
    changes: dict[str, object],
) -> None:
    admin = identities["admin"]
    await inserted(client, session_factory, "admin-2@test.example", Role.ADMIN)

    response = await client.patch(f"{USERS}/{admin.id}", json=changes, headers=admin.headers)

    assert response.status_code == 422
    refused(response.json(), "You cannot deactivate or demote yourself.")
    row = await user_row(session_factory, admin.id)
    assert (row.role, row.is_active) == (Role.ADMIN, True)
    assert await user_audit_rows(session_factory) == []


async def test_demoting_another_admin_is_allowed_while_one_active_admin_remains(
    client: AsyncClient, admin: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    other = await inserted(client, session_factory, "admin-2@test.example", Role.ADMIN)

    response = await client.patch(
        f"{USERS}/{other.id}", json={"role": "USER"}, headers=admin.headers
    )
    lost_rights = await client.get(USERS, headers=other.headers)

    assert response.status_code == 200
    assert lost_rights.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"email": "new@test.example"},
        {"email": "new@test.example", "role": "VIEWER"},
    ],
    ids=["email only", "email with a role change"],
)
async def test_email_is_immutable(
    client: AsyncClient,
    admin: Identity,
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    body: dict[str, object],
) -> None:
    response = await client.patch(f"{USERS}/{user_a.id}", json=body, headers=admin.headers)

    assert response.status_code == 422
    refused(response.json(), "email cannot be changed")
    row = await user_row(session_factory, user_a.id)
    assert (row.email, row.role) == (user_a.email, Role.USER)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"role": None},
        {"is_active": None},
        {"role": "ROOT"},
        {"is_active": "false"},
        {"password": "new-password-123"},
        {"password_hash": "$argon2id$x"},
    ],
)
async def test_a_patch_outside_role_and_is_active_is_422(
    client: AsyncClient,
    admin: Identity,
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
    body: dict[str, object],
) -> None:
    response = await client.patch(f"{USERS}/{user_a.id}", json=body, headers=admin.headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    row = await user_row(session_factory, user_a.id)
    assert (row.role, row.is_active) == (Role.USER, True)


async def test_a_patch_that_changes_nothing_writes_no_audit_row(
    client: AsyncClient,
    admin: Identity,
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.patch(
        f"{USERS}/{user_a.id}", json={"role": "USER", "is_active": True}, headers=admin.headers
    )

    assert response.status_code == 200
    assert await user_audit_rows(session_factory) == []


async def test_an_unknown_user_is_404(client: AsyncClient, admin: Identity) -> None:
    response = await client.patch(f"{USERS}/999999", json={"role": "USER"}, headers=admin.headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_users_are_never_deleted(
    client: AsyncClient,
    admin: Identity,
    user_a: Identity,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.delete(f"{USERS}/{user_a.id}", headers=admin.headers)

    assert response.status_code == 405
    assert (await user_row(session_factory, user_a.id)).email == user_a.email
