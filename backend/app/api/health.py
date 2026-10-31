import logging
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import literal, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    state: HealthState
    queued: int = 0
    in_flight: int = 0
    retry_pending: int = 0
    total_uncommitted: int = 0
    retry_count: int = 0
    oldest_pending_age_s: float | None = None
    last_commit_at: datetime | None = None
    writer_state: Literal["NOT_STARTED"] = "NOT_STARTED"
    active_connections: int = 0


@router.get("/live")
async def live() -> Liveness:
    return Liveness()


@router.get("/ready", responses={503: {"model": Readiness}})
async def ready(
    response: Response, session: Annotated[AsyncSession, Depends(get_session)]
) -> Readiness:
    try:
        await session.execute(select(literal(1)))
    except SQLAlchemyError as exc:
        logger.warning("readiness database check failed", extra={"error": type(exc).__name__})
        response.status_code = 503
        return Readiness(state=HealthState.DEGRADED)
    return Readiness(state=HealthState.HEALTHY)
