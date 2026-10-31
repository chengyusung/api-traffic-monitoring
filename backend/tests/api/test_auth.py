import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.db import get_session, make_engine
from app.main import app
from app.models import Role, SystemLog, User
from tests.conftest import PASSWORD, Identity

REGISTER = "/api/v1/auth/register"
TOKEN = "/api/v1/auth/token"
ME = "/api/v1/auth/me"


def error(response: Response, status: int, code: str) -> dict[str, str]:
    assert response.status_code == status, response.text
    body = response.json()["error"]
    assert body["code"] == code
    assert body["request_id"] == response.headers["X-Request-ID"]
    if status == 401:
        assert response.headers.get("WWW-Authenticate") == "Bearer"
    return body


async def audit_rows(
    session_factory: async_sessionmaker[AsyncSession], event_type: str
) -> list[SystemLog]:
    async with session_factory() as session:
        rows = await session.scalars(select(SystemLog).where(SystemLog.event_type == event_type))
        return list(rows)


async def test_register_creates_an_active_user_with_lowercased_email(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        REGISTER, json={"email": " New@Example.COM ", "password": PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["role"] == "USER"
    assert body["is_active"] is True
    [row] = await audit_rows(session_factory, "auth.register")
    assert row.actor_id == body["id"]
    assert row.request_id == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "extra", [{"role": "ADMIN"}, {"is_active": False}, {"password_hash": "$argon2id$x"}]
)
async def test_register_rejects_privilege_fields(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], extra: dict[str, object]
) -> None:
    response = await client.post(
        REGISTER, json={"email": "mallory@example.com", "password": PASSWORD, **extra}
    )

    error(response, 422, "VALIDATION_ERROR")
    async with session_factory() as session:
        assert await session.scalar(select(User.id)) is None


@pytest.mark.parametrize("password", ["1234567", "p" * 129], ids=["7 chars", "129 chars"])
async def test_register_rejects_password_length_outside_8_to_128(
    client: AsyncClient, password: str
) -> None:
    response = await client.post(REGISTER, json={"email": "a@example.com", "password": password})

    error(response, 422, "VALIDATION_ERROR")


async def test_register_duplicate_email_is_409_case_insensitively(
    client: AsyncClient, user_a: Identity
) -> None:
    response = await client.post(
        REGISTER, json={"email": user_a.email.upper(), "password": PASSWORD}
    )

    error(response, 409, "CONFLICT")


async def test_login_returns_a_bearer_token_and_audits_success(
    client: AsyncClient, user_a: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.get(ME, headers=user_a.headers)

    assert response.status_code == 200
    assert response.json()["email"] == user_a.email
    [row] = await audit_rows(session_factory, "auth.login")
    assert row.actor_id == user_a.id
    assert row.context == {"email": user_a.email, "result": "success"}


async def test_token_claims_follow_adr_002(user_a: Identity) -> None:
    token = user_a.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, options={"verify_signature": False})

    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    assert claims.keys() == {"sub", "role", "exp", "iat", "jti"}
    assert claims["sub"] == str(user_a.id)
    assert claims["exp"] - claims["iat"] == 8 * 3600


async def test_login_failures_are_identical_401s_and_audited(
    client: AsyncClient, user_a: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    wrong_password = await client.post(
        TOKEN, data={"username": user_a.email, "password": "wrong-password"}
    )
    unknown_email = await client.post(
        TOKEN, data={"username": "nobody@example.com", "password": PASSWORD}
    )

    first = error(wrong_password, 401, "UNAUTHORIZED")
    second = error(unknown_email, 401, "UNAUTHORIZED")
    assert first["message"] == second["message"]
    failures = [
        row.context
        for row in await audit_rows(session_factory, "auth.login")
        if row.context and row.context["result"] == "failure"
    ]
    assert failures == [
        {"email": user_a.email, "result": "failure"},
        {"email": "nobody@example.com", "result": "failure"},
    ]
    assert PASSWORD not in json.dumps(failures)


async def test_login_normalizes_the_email_like_register(
    client: AsyncClient, user_a: Identity
) -> None:
    response = await client.post(
        TOKEN, data={"username": f"  {user_a.email.upper()} ", "password": PASSWORD}
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "identifier", ["not-an-email", "x" * 250 + "@example.com"], ids=["no @", "over 255"]
)
async def test_failed_login_with_a_non_email_identifier_audits_a_placeholder(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], identifier: str
) -> None:
    response = await client.post(TOKEN, data={"username": identifier, "password": PASSWORD})

    error(response, 401, "UNAUTHORIZED")
    [row] = await audit_rows(session_factory, "auth.login")
    assert row.context == {"email": "<invalid-email>", "result": "failure"}


async def test_inactive_user_cannot_log_in_or_use_an_old_token(
    client: AsyncClient, user_a: Identity, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        user = await session.get(User, user_a.id)
        assert user is not None
        user.is_active = False
        await session.commit()

    login = await client.post(TOKEN, data={"username": user_a.email, "password": PASSWORD})
    me = await client.get(ME, headers=user_a.headers)

    error(login, 401, "UNAUTHORIZED")
    error(me, 401, "UNAUTHORIZED")


async def test_a_corrupt_stored_hash_is_a_failed_login(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        session.add(User(email="corrupt@example.com", password_hash="not-argon2", role=Role.USER))
        await session.commit()

    response = await client.post(
        TOKEN, data={"username": "corrupt@example.com", "password": PASSWORD}
    )

    error(response, 401, "UNAUTHORIZED")


def b64(segment: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(segment).encode()).rstrip(b"=").decode()


def unsigned_token(user_id: int) -> str:
    now = int(datetime.now(UTC).timestamp())
    claims = {"sub": str(user_id), "role": "ADMIN", "iat": now, "exp": now + 600, "jti": "x"}
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."


def foreign_token(user_id: int) -> str:
    now = datetime.now(UTC)
    claims = {"sub": str(user_id), "role": "ADMIN", "iat": now, "exp": now + timedelta(hours=1)}
    return jwt.encode({**claims, "jti": "x"}, "a-different-secret-of-32-bytes-or-more", "HS256")


@pytest.mark.parametrize(
    ("make_headers", "code"),
    [
        (lambda _: {}, "UNAUTHORIZED"),
        (lambda _: {"Authorization": "Bearer not-a-jwt"}, "UNAUTHORIZED"),
        (lambda _: {"Authorization": "Basic dXNlcjpwYXNz"}, "UNAUTHORIZED"),
        (lambda uid: {"Authorization": f"Bearer {foreign_token(uid)}"}, "UNAUTHORIZED"),
        (lambda uid: {"Authorization": f"Bearer {unsigned_token(uid)}"}, "UNAUTHORIZED"),
        (
            lambda uid: {
                "Authorization": "Bearer "
                + create_access_token(uid, Role.USER, now=datetime.now(UTC) - timedelta(hours=9))
            },
            "TOKEN_EXPIRED",
        ),
    ],
    ids=["no token", "malformed", "not bearer", "wrong signature", "alg none", "expired"],
)
async def test_bad_tokens_are_401(
    client: AsyncClient, user_a: Identity, make_headers: Callable[[int], dict[str, str]], code: str
) -> None:
    response = await client.get(ME, headers=make_headers(user_a.id))

    error(response, 401, code)


async def test_authorization_uses_the_database_role_not_the_token_role(
    client: AsyncClient, viewer: Identity, services: dict[str, int]
) -> None:
    claims_admin = {"Authorization": f"Bearer {create_access_token(viewer.id, Role.ADMIN)}"}
    record = {
        "service_slug": "auth",
        "title": "escalation attempt",
        "value": 1,
        "observed_at": "2026-10-31T00:00:00Z",
    }

    response = await client.post("/api/v1/records", json=record, headers=claims_admin)

    error(response, 403, "FORBIDDEN")


@pytest.mark.parametrize(
    "send",
    [
        lambda client, user: client.get(ME, headers=user.headers),
        lambda client, user: client.post(
            TOKEN, data={"username": user.email, "password": PASSWORD}
        ),
    ],
    ids=["GET /auth/me", "POST /auth/token"],
)
async def test_database_outage_is_503_not_a_pass(
    client: AsyncClient,
    user_a: Identity,
    caplog: pytest.LogCaptureFixture,
    send: Callable[[AsyncClient, Identity], Awaitable[Response]],
) -> None:
    unreachable = make_engine("mysql+asyncmy://traffic:wrong@127.0.0.1:1/traffic_test")

    async def unreachable_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(unreachable) as session:
            yield session

    app.dependency_overrides[get_session] = unreachable_session
    response = await send(client, user_a)
    await unreachable.dispose()

    error(response, 503, "SERVICE_UNAVAILABLE")
    logged = {(r.levelname, getattr(r, "error", None)) for r in caplog.records}
    assert ("WARNING", "OperationalError") in logged
