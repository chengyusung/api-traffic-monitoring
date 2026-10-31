import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap import bootstrap_admin, ensure_services, seed_demo
from app.core.security import verify_password
from app.models import ApiService, Role, User

SECRET = SecretStr("bootstrap-pass-1")


async def users_by_email(session: AsyncSession) -> dict[str, Role]:
    return dict((await session.execute(select(User.email, User.role))).all())


async def test_ensure_services_is_idempotent_and_keeps_admin_thresholds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await ensure_services(session) == ["auth", "orders", "payment"]
        payment = await session.scalar(select(ApiService).where(ApiService.slug == "payment"))
        assert payment is not None
        payment.alert_threshold = 95
        await session.commit()

        assert await ensure_services(session) == []
        rows = await session.execute(select(ApiService.slug, ApiService.alert_threshold))
        assert dict(rows.all()) == {"auth": 80, "orders": 150, "payment": 95}


async def test_bootstrap_admin_runs_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await bootstrap_admin(session, " Admin@Example.com ", SECRET) is True
        assert await bootstrap_admin(session, "second@example.com", SECRET) is False

        assert await users_by_email(session) == {"admin@example.com": Role.ADMIN}
        admin = await session.scalar(select(User))
        assert admin is not None
        assert verify_password(admin.password_hash, SECRET.get_secret_value())


async def test_bootstrap_admin_refuses_to_promote_an_existing_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(User(email="admin@example.com", password_hash="x", role=Role.USER))
        await session.commit()

        with pytest.raises(RuntimeError, match="refusing to promote"):
            await bootstrap_admin(session, "admin@example.com", SECRET)
        assert await users_by_email(session) == {"admin@example.com": Role.USER}


async def test_bootstrap_admin_without_config_fails_loudly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="ADMIN_EMAIL"):
            await bootstrap_admin(session, None, None)


async def test_seed_demo_is_idempotent_by_email(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    accounts = [
        (Role.USER, "user@example.com", SECRET),
        (Role.VIEWER, "viewer@example.com", SECRET),
    ]
    async with session_factory() as session:
        assert await seed_demo(session, accounts) == ["user@example.com", "viewer@example.com"]
        assert await seed_demo(session, accounts) == []

        assert await users_by_email(session) == {
            "user@example.com": Role.USER,
            "viewer@example.com": Role.VIEWER,
        }
