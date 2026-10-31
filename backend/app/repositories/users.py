from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User


async def get(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email))


async def admin_exists(session: AsyncSession) -> bool:
    return await session.scalar(select(User.id).where(User.role == Role.ADMIN).limit(1)) is not None


async def list_page(session: AsyncSession, page: int, page_size: int) -> tuple[Sequence[User], int]:
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    rows = await session.scalars(
        select(User).order_by(User.id).offset((page - 1) * page_size).limit(page_size)
    )
    return rows.all(), total


async def lock_active_admin_ids(session: AsyncSession) -> set[int]:
    """Lock the active ADMIN rows until commit. Two admins demoting each other at once queue
    here, and the second one counts the first one's committed change."""
    rows = await session.scalars(
        select(User.id).where(User.role == Role.ADMIN, User.is_active.is_(True)).with_for_update()
    )
    return set(rows)


async def get_for_update(session: AsyncSession, user_id: int) -> User | None:
    """The current row, locked, even when the identity map holds an older copy."""
    return await session.get(User, user_id, with_for_update=True, populate_existing=True)
