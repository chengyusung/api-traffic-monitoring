from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import Admin, CurrentPipeline, Session
from app.models import AuditEvent
from app.schemas import Pagination
from app.schemas.admin import LogPage, LogQuery, SystemStatus, UserPage, UserUpdate
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


@router.get("/logs")
async def list_logs(query: Annotated[LogQuery, Query()], _: Admin, session: Session) -> LogPage:
    return await admin.list_logs(session, query)


@router.get("/logs/event-types")
async def list_event_types(_: Admin) -> list[AuditEvent]:
    """The values the event_type filter of GET /admin/logs accepts."""
    return list(AuditEvent)


@router.get("/system")
async def system_status(_: Admin, session: Session, pipeline: CurrentPipeline) -> SystemStatus:
    """/health/ready's counters plus pool, simulator and table state. It needs the database
    to authenticate, so during an outage it is a 503 like every other authenticated route."""
    return await admin.system_status(session, pipeline)
