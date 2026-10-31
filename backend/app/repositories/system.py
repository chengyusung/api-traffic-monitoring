from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base, SystemLog
from app.schemas.admin import LogQuery


async def log_page(session: AsyncSession, query: LogQuery) -> tuple[Sequence[SystemLog], int]:
    conditions: list[ColumnElement[bool]] = []
    if query.level is not None:
        conditions.append(SystemLog.level == query.level)
    if query.event_type is not None:
        conditions.append(SystemLog.event_type == query.event_type)
    if query.start is not None:
        conditions.append(SystemLog.created_at >= query.start)
    if query.end is not None:
        conditions.append(SystemLog.created_at < query.end)
    total = await session.scalar(select(func.count()).select_from(SystemLog).where(*conditions))
    rows = await session.scalars(
        select(SystemLog)
        .where(*conditions)
        .order_by(SystemLog.created_at.desc(), SystemLog.id.desc())
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    return rows.all(), total or 0


async def row_counts(session: AsyncSession) -> dict[str, int]:
    """Every application table's row count in one round trip."""
    tables = Base.metadata.sorted_tables
    counts = select(
        *(select(func.count()).select_from(t).scalar_subquery().label(t.name) for t in tables)
    )
    return dict((await session.execute(counts)).one()._mapping)
