import logging
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import literal, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import CurrentPipeline, Session
from app.ingestion.buffer import WriterState
from app.schemas.realtime import HealthState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    state: HealthState
    queued: int
    in_flight: int
    retry_pending: int
    total_uncommitted: int
    retry_count: int
    oldest_pending_age_s: float | None
    last_commit_at: datetime | None
    writer_state: WriterState
    active_connections: int


@router.get("/live")
async def live() -> Liveness:
    return Liveness()


@router.get("/ready", responses={503: {"model": Readiness}})
async def ready(response: Response, session: Session, pipeline: CurrentPipeline) -> Readiness:
    """Unauthenticated on purpose: it must stay readable while the database is down."""
    try:
        await session.execute(select(literal(1)))
        db_ok = True
    except SQLAlchemyError as exc:
        logger.warning("readiness database check failed", extra={"error": type(exc).__name__})
        db_ok = False
    readiness = Readiness(
        state=pipeline.state(db_ok=db_ok),
        active_connections=pipeline.manager.active_connections,
        **asdict(pipeline.writer.stats()),
    )
    if readiness.state is not HealthState.HEALTHY:
        response.status_code = 503
    return readiness
