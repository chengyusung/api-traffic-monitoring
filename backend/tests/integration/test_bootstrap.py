import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap import (
    DEMO_RECORDS,
    bootstrap_admin,
    ensure_services,
    seed_demo,
    seed_demo_records,
)
from app.core.security import verify_password
from app.models import (
    ApiService,
    ImportBatch,
    ImportStatus,
    RecordSource,
    Role,
    TrafficRecord,
    User,
)

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
        assert await verify_password(admin.password_hash, SECRET.get_secret_value())


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


@pytest.mark.parametrize(
    ("email", "password"),
    [("admin@example.com", ""), ("admin@example.com", "s3cr3t"), ("not-an-email", "long-enough-1")],
    ids=["empty password", "short password", "malformed email"],
)
async def test_bootstrap_admin_rejects_what_register_rejects(
    session_factory: async_sessionmaker[AsyncSession], email: str, password: str
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValidationError) as rejected:
            await bootstrap_admin(session, email, SecretStr(password))
        assert await users_by_email(session) == {}
    assert "s3cr3t" not in str(rejected.value)


async def test_seed_demo_rejects_an_empty_password(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    accounts = [
        (Role.USER, "user@example.com", SECRET),
        (Role.VIEWER, "viewer@example.com", SecretStr("")),
    ]
    async with session_factory() as session:
        with pytest.raises(ValidationError):
            await seed_demo(session, accounts)
    async with session_factory() as session:
        assert await users_by_email(session) == {}


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


async def test_seed_demo_records_imports_the_sample_once_as_the_demo_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await ensure_services(session)
        await seed_demo(session, [(Role.USER, "User@Example.com", SECRET)])

        batch_id = await seed_demo_records(session, "User@Example.com")
        assert batch_id is not None
        assert await seed_demo_records(session, "User@Example.com") is None

    async with session_factory() as session:
        batches = (await session.scalars(select(ImportBatch))).all()
        owners = set(
            (
                await session.execute(
                    select(TrafficRecord.source, User.email).join(
                        User, TrafficRecord.created_by == User.id
                    )
                )
            ).all()
        )
    assert [(b.id, b.filename, b.status) for b in batches] == [
        (batch_id, "seed:sample_traffic.csv", ImportStatus.SUCCEEDED)
    ]
    assert batches[0].success_count == len(DEMO_RECORDS.read_text().splitlines()) - 1
    assert owners == {(RecordSource.IMPORT, "user@example.com")}
