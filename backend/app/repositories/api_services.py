from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiService


async def list_all(session: AsyncSession) -> Sequence[ApiService]:
    return (await session.scalars(select(ApiService).order_by(ApiService.id))).all()


async def get(session: AsyncSession, service_id: int) -> ApiService | None:
    return await session.get(ApiService, service_id)


async def get_by_slugs(session: AsyncSession, slugs: Iterable[str]) -> dict[str, ApiService]:
    rows = await session.scalars(select(ApiService).where(ApiService.slug.in_(set(slugs))))
    return {service.slug: service for service in rows}
