"""Startup steps run by entrypoint.sh after `alembic upgrade head`. Each step is idempotent."""

import asyncio
import logging
from pathlib import Path

from pydantic import SecretStr, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.security import hash_password
from app.db import SessionLocal, engine
from app.models import ApiService, RecordSource, Role, TrafficRecord, User
from app.repositories import api_services, users
from app.schemas.auth import Email, UserCreate
from app.services import imports

logger = logging.getLogger("app.bootstrap")

# architecture.md §5: slug, display name, initial alert_threshold.
SERVICES = (("auth", "Auth API", 80), ("orders", "Orders API", 150), ("payment", "Payment API", 80))
# The repository's data/; docker-compose.yml mounts it at the same relative place.
DEMO_RECORDS = Path(__file__).resolve().parents[2] / "data" / "sample_traffic.csv"


async def ensure_services(session: AsyncSession) -> list[str]:
    """Insert missing services. Existing rows keep the threshold an admin may have set."""
    existing = await api_services.get_by_slugs(session, (slug for slug, _, _ in SERVICES))
    missing = [row for row in SERVICES if row[0] not in existing]
    session.add_all(
        ApiService(slug=slug, display_name=name, alert_threshold=threshold)
        for slug, name, threshold in missing
    )
    await session.commit()
    return [slug for slug, _, _ in missing]


async def bootstrap_admin(
    session: AsyncSession, email: str | None, password: SecretStr | None
) -> bool:
    """Create the first ADMIN only while no ADMIN exists, so a restart never adds another."""
    if await users.admin_exists(session):
        return False
    if not email or password is None:
        raise RuntimeError("No ADMIN exists and ADMIN_EMAIL / ADMIN_PASSWORD are not set.")
    # Same email and password rules as POST /auth/register; a ValidationError stops startup.
    account = UserCreate(email=email, password=password.get_secret_value())
    if await users.get_by_email(session, account.email) is not None:
        # Promoting an account someone else registered would hand them admin rights.
        raise RuntimeError(
            "ADMIN_EMAIL belongs to an existing non-admin user; refusing to promote."
        )
    session.add(
        User(
            email=account.email,
            password_hash=await hash_password(account.password),
            role=Role.ADMIN,
        )
    )
    await session.commit()
    return True


async def seed_demo(
    session: AsyncSession, accounts: list[tuple[Role, str | None, SecretStr | None]]
) -> list[str]:
    created = []
    for role, email, password in accounts:
        if not email or password is None:
            raise RuntimeError(f"SEED_DEMO_DATA=true needs DEMO_{role}_EMAIL and _PASSWORD.")
        account = UserCreate(email=email, password=password.get_secret_value())
        if await users.get_by_email(session, account.email) is None:
            session.add(
                User(
                    email=account.email,
                    password_hash=await hash_password(account.password),
                    role=role,
                )
            )
            created.append(account.email)
    await session.commit()
    return created


async def seed_demo_records(session: AsyncSession, user_email: str | None) -> int | None:
    """Import the sample CSV as the demo USER unless any IMPORT row exists; returns the batch id."""
    imported = select(TrafficRecord.id).where(TrafficRecord.source == RecordSource.IMPORT)
    if await session.scalar(imported.limit(1)) is not None:
        return None
    user = await users.get_by_email(session, TypeAdapter(Email).validate_python(user_email))
    if user is None:
        raise RuntimeError("SEED_DEMO_DATA=true but the demo USER does not exist.")
    content = await asyncio.to_thread(DEMO_RECORDS.read_bytes)
    batch = await imports.import_file(session, f"seed:{DEMO_RECORDS.name}", content, user)
    return batch.id


async def main() -> None:
    setup_logging(settings.log_level)
    async with SessionLocal() as session:
        logger.info("ensure_services", extra={"inserted": await ensure_services(session)})

        inserted = await bootstrap_admin(session, settings.admin_email, settings.admin_password)
        logger.info("bootstrap_admin", extra={"inserted": inserted})

        if settings.seed_demo_data:
            inserted_emails = await seed_demo(
                session,
                [
                    (Role.USER, settings.demo_user_email, settings.demo_user_password),
                    (Role.VIEWER, settings.demo_viewer_email, settings.demo_viewer_password),
                ],
            )
            logger.info("seed_demo", extra={"inserted": inserted_emails})
            batch_id = await seed_demo_records(session, settings.demo_user_email)
            logger.info("seed_demo_records", extra={"import_batch_id": batch_id})
        else:
            logger.info("seed_demo", extra={"skipped": "SEED_DEMO_DATA is false"})
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
