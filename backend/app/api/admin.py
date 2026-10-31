from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import Admin, Session
from app.schemas import Pagination
from app.schemas.admin import UserPage, UserUpdate
from app.schemas.auth import UserRead
from app.services import admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users")
async def list_users(query: Annotated[Pagination, Query()], _: Admin, session: Session) -> UserPage:
    return await admin.list_users(session, query)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int, changes: UserUpdate, actor: Admin, session: Session
) -> UserRead:
    """Role and is_active only. 422 for an email change, for the actor's own deactivation or
    demotion, and for the last active ADMIN. There is no delete."""
    return UserRead.model_validate(await admin.update_user(session, user_id, changes, actor))
