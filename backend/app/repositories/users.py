from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User


async def get(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email))


async def admin_exists(session: AsyncSession) -> bool:
    return await session.scalar(select(User.id).where(User.role == Role.ADMIN).limit(1)) is not None
