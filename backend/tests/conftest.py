import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.bootstrap import ensure_services
from app.core.security import hash_password
from app.db import get_session, make_engine
from app.main import app
from app.models import ApiService, Base, Role, User

TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]
PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = TEST_DATABASE_URL
    return config


@pytest.fixture(scope="session")
def migrated(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")


@pytest.fixture
async def engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(TEST_DATABASE_URL)
    yield engine
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def no_credentials_in_response(response: Response) -> None:
    await response.aread()
    assert "$argon2" not in response.text, "a password hash reached a response"
    if response.is_success:
        assert "password_hash" not in response.text, "a success response exposes password_hash"


@pytest.fixture
async def client(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        event_hooks={"response": [no_credentials_in_response]},
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
async def services(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Slug to id for the three seeded services."""
    async with session_factory() as session:
        await ensure_services(session)
        rows = await session.execute(select(ApiService.slug, ApiService.id))
        return dict(rows.all())


@dataclass(frozen=True)
class Identity:
    id: int
    email: str
    headers: dict[str, str]


async def log_in(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/token", data={"username": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def registered(client: AsyncClient, email: str) -> Identity:
    """A USER created the only way the public API allows: POST /auth/register."""
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return Identity(response.json()["id"], email, await log_in(client, email))


async def inserted(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], email: str, role: Role
) -> Identity:
    """ADMIN and VIEWER cannot come from registration, so they are inserted like bootstrap does."""
    async with session_factory() as session:
        user = User(email=email, password_hash=hash_password(PASSWORD), role=role)
        session.add(user)
        await session.commit()
    return Identity(user.id, email, await log_in(client, email))


@pytest.fixture
async def admin(client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> Identity:
    return await inserted(client, session_factory, "admin@test.example", Role.ADMIN)


@pytest.fixture
async def viewer(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> Identity:
    return await inserted(client, session_factory, "viewer@test.example", Role.VIEWER)


@pytest.fixture
async def user_a(client: AsyncClient) -> Identity:
    return await registered(client, "user-a@test.example")


@pytest.fixture
async def user_b(client: AsyncClient) -> Identity:
    return await registered(client, "user-b@test.example")


@pytest.fixture
def identities(
    admin: Identity, user_a: Identity, user_b: Identity, viewer: Identity
) -> dict[str, Identity]:
    """The four ADR-002 identities by fixture name, for tests parametrized over actors."""
    return {"admin": admin, "user_a": user_a, "user_b": user_b, "viewer": viewer}
