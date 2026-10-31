"""Admin operations (ADR-002 Admin 管理規則)."""

from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, UnprocessableError
from app.models import AuditEvent, Role, User
from app.repositories import users
from app.schemas import Pagination
from app.schemas.admin import UserPage, UserUpdate
from app.schemas.auth import UserRead
from app.services import audit


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
    active ADMIN; the audit row commits with the change."""
    # End the transaction that authenticated the actor: MariaDB 11.7 rejects a locking read
    # of a row changed after that transaction's snapshot (1020, innodb_snapshot_isolation).
    await session.commit()
    # Lock first, in one order for every caller, so concurrent changes cannot deadlock.
    active_admins = await users.lock_active_admin_ids(session)
    target = await users.get_for_update(session, user_id)
    if target is None:
        raise NotFoundError("User not found.")
    after = {
        name: value
        for name, value in changes.model_dump(exclude_unset=True).items()
        if getattr(target, name) != value
    }
    if not after:
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
