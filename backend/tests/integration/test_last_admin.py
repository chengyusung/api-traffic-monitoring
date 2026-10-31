"""ADR-002: never the last active ADMIN, even when two admins act at the same moment."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ForbiddenError
from app.models import Role, User
from app.schemas.admin import UserUpdate
from app.services import admin


async def two_admins(session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with session_factory() as session:
        first = User(email="a@test.example", password_hash="x", role=Role.ADMIN)
        second = User(email="b@test.example", password_hash="x", role=Role.ADMIN)
        session.add_all([first, second])
        await session.commit()
        return first.id, second.id


async def active_admins(session_factory: async_sessionmaker[AsyncSession]) -> set[int]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(User.id).where(User.role == Role.ADMIN, User.is_active.is_(True))
        )
        return set(rows)


async def test_an_admin_demoted_mid_request_cannot_demote_the_last_active_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await two_admins(session_factory)
    async with session_factory() as session:
        # B's request authenticated as ADMIN; its transaction has read B's row already.
        actor = await session.get(User, b)
        assert actor is not None and actor.role == Role.ADMIN
        async with session_factory() as other:
            demoted = await other.get(User, b)
            assert demoted is not None
            demoted.role = Role.USER
            await other.commit()

        # Refused as a non-admin, before the last-admin rule is reached.
        with pytest.raises(ForbiddenError):
            await admin.update_user(session, a, UserUpdate(role=Role.VIEWER), actor)

    assert await active_admins(session_factory) == {a}


async def test_two_admins_demoting_each_other_at_once_leave_one_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await two_admins(session_factory)
    async with session_factory() as first, session_factory() as second:
        actor_a, actor_b = await first.get(User, a), await second.get(User, b)
        assert actor_a is not None and actor_b is not None

        results = await asyncio.gather(
            admin.update_user(first, b, UserUpdate(role=Role.USER), actor_a),
            admin.update_user(second, a, UserUpdate(role=Role.USER), actor_b),
            return_exceptions=True,
        )

    # The second request waits for the first commit, then finds its actor demoted.
    assert sorted(type(result).__name__ for result in results) == ["ForbiddenError", "User"]
    assert len(await active_admins(session_factory)) == 1


async def test_an_admin_demoted_mid_request_can_no_longer_change_anyone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await two_admins(session_factory)
    async with session_factory() as session:
        third = User(email="c@test.example", password_hash="x", role=Role.ADMIN)
        session.add(third)
        await session.commit()
    async with session_factory() as session:
        actor = await session.get(User, b)
        assert actor is not None
        async with session_factory() as other:
            demoted = await other.get(User, b)
            assert demoted is not None
            demoted.role = Role.USER
            await other.commit()

        # With three admins the last-admin rule does not apply; only the actor check does.
        with pytest.raises(ForbiddenError):
            await admin.update_user(session, third.id, UserUpdate(role=Role.VIEWER), actor)

    assert await active_admins(session_factory) == {a, third.id}


async def test_a_change_that_changes_nothing_releases_its_locks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await two_admins(session_factory)
    async with session_factory() as session, session_factory() as other:
        actor = await session.get(User, a)
        assert actor is not None
        unchanged = await admin.update_user(session, b, UserUpdate(role=Role.ADMIN), actor)
        locked = await other.scalars(
            select(User.id).where(User.role == Role.ADMIN).with_for_update(nowait=True)
        )

        assert (unchanged.role, set(locked)) == (Role.ADMIN, {a, b})


async def test_an_unsaved_change_in_the_session_is_never_committed_by_update_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await two_admins(session_factory)
    async with session_factory() as session:
        actor = await session.get(User, a)
        assert actor is not None
        actor.email = "renamed@test.example"

        with pytest.raises(AssertionError, match="pending"):
            await admin.update_user(session, b, UserUpdate(role=Role.USER), actor)

    async with session_factory() as session:
        stored = await session.get(User, a)
    assert stored is not None and stored.email == "a@test.example"
