from collections.abc import AsyncIterator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.errors import ConflictError


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        # Bound values (emails, password hashes) stay out of exception text and logs.
        hide_parameters=True,
        connect_args={
            # Runs on every new physical connection, not once at startup.
            "init_command": "SET time_zone='+00:00'",
            # A black-holed connection fails as a lost connection instead of hanging forever.
            "read_timeout": settings.commit_timeout_seconds,
        },
    )


engine = make_engine(settings.database_url.get_secret_value())
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def is_duplicate_key(exc: IntegrityError) -> bool:
    """MariaDB ER_DUP_ENTRY only; FK or CHECK violations are bugs, not duplicates."""
    return exc.orig is not None and exc.orig.args[0] == 1062


async def flush_or_conflict(session: AsyncSession, message: str) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        if is_duplicate_key(exc):
            raise ConflictError(message) from exc
        raise
