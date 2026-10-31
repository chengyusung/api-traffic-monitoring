"""Admin operations (ADR-002 Admin 管理規則)."""

import time
from collections.abc import Callable
from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ForbiddenError, NotFoundError, UnprocessableError
from app.models import AuditEvent, Role, User
from app.pipeline import Pipeline
from app.repositories import system, users
from app.schemas import Pagination
from app.schemas.admin import (
    GeneratorStatus,
    LogPage,
    LogQuery,
    LogRead,
    PoolStatus,
    SystemStatus,
    UserPage,
    UserUpdate,
)
from app.schemas.auth import UserRead
from app.services import audit

TABLE_COUNTS_TTL_S = 10.0
# One entry for the process: COUNT(*) reads whole tables, and the Admin page reruns often.
_table_counts: dict[str, Any] = {}


async def list_users(session: AsyncSession, query: Pagination) -> UserPage:
    rows, total = await users.list_page(session, query.page, query.page_size)
    return UserPage(
        items=[UserRead.model_validate(row) for row in rows],
        page=query.page,
        page_size=query.page_size,
        total=total,
    )


async def update_user(
    session: AsyncSession, user_id: int, changes: UserUpdate, actor: User
) -> User:
    """Change role and / or is_active. Refuses to leave the actor or the system without an
    active ADMIN; the audit row commits with the change. The session must hold no unsaved
    change: the first commit below would write it."""
    assert not (session.new or session.dirty or session.deleted), "pending changes in session"
    # End the transaction that authenticated the actor: MariaDB 11.7 rejects a locking read
    # of a row changed after that transaction's snapshot (1020, innodb_snapshot_isolation).
    await session.commit()
    # Lock first, in one order for every caller, so concurrent changes cannot deadlock.
    active_admins = await users.lock_active_admin_ids(session)
    # Authentication read the actor before the locks; another admin may have demoted them since.
    if actor.id not in active_admins:
        raise ForbiddenError("Only an active ADMIN can change users.")
    target = await users.get_for_update(session, user_id)
    if target is None:
        raise NotFoundError("User not found.")
    after = {
        name: value
        for name, value in changes.model_dump(exclude_unset=True).items()
        if getattr(target, name) != value
    }
    if not after:
        # Release the row locks now, not when the request's session closes.
        await session.rollback()
        await session.refresh(target)
        return target
    stays_admin = after.get("role", target.role) == Role.ADMIN and after.get(
        "is_active", target.is_active
    )
    if not stays_admin and target.id == actor.id:
        raise UnprocessableError("You cannot deactivate or demote yourself.")
    if not stays_admin and active_admins == {target.id}:
        raise UnprocessableError("The last active ADMIN cannot be deactivated or demoted.")
    before = {name: getattr(target, name) for name in after}
    for name, value in after.items():
        setattr(target, name, value)
    audit.record_event(
        session,
        AuditEvent.USER_UPDATED,
        f"user {target.id} updated: {', '.join(after)}",
        actor_id=actor.id,
        target_type="user",
        target_id=target.id,
        context=to_jsonable_python({"before": before, "after": after}),
    )
    await session.commit()
    return target


async def list_logs(session: AsyncSession, query: LogQuery) -> LogPage:
    rows, total = await system.log_page(session, query)
    return LogPage(
        items=[LogRead.model_validate(row) for row in rows],
        page=query.page,
        page_size=query.page_size,
        total=total,
    )


async def _cached_table_counts(session: AsyncSession, now: float) -> dict[str, int]:
    if not _table_counts or now - _table_counts["at"] >= TABLE_COUNTS_TTL_S:
        _table_counts.update(at=now, counts=await system.row_counts(session))
    return _table_counts["counts"]


async def system_status(
    session: AsyncSession, pipeline: Pipeline, clock: Callable[[], float] = time.monotonic
) -> SystemStatus:
    generator = pipeline.generator
    return SystemStatus(
        # The actor was just loaded through this session, so the database is answering;
        # during an outage the request is a 503 before it gets here (ADR-002).
        readiness=pipeline.readiness(db_ok=True),
        pool=PoolStatus(
            status=session.get_bind().engine.pool.status(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        ),
        generator=GeneratorStatus(
            thresholds=None
            if generator.services is None
            else {service.slug: service.threshold for service in generator.services},
            thresholds_loaded_at=generator.thresholds_loaded_at,
            paused_s=generator.paused_s,
            stopped_by=generator.stopped_by,
        ),
        table_counts=await _cached_table_counts(session, clock()),
    )
