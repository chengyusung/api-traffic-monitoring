"""WebSocket wire schema (ADR-003). Pushed before commit, so no id and no persisted_at."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ingestion.buffer import WriterState
from app.models import RecordSource
from app.schemas import NonNegativeInt
from app.schemas.records import UTCInstant


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class TrafficObserved(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    type: Literal["traffic.observed"] = "traffic.observed"
    event_id: uuid.UUID
    service_slug: str
    title: str
    value: NonNegativeInt
    source: RecordSource
    observed_at: UTCInstant
    threshold_applied: NonNegativeInt
    is_anomaly: bool


class StatusMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["status"] = "status"
    state: HealthState
    total_uncommitted: int


class Readiness(BaseModel):
    """/health/ready, and the readiness part of /admin/system (architecture.md §9)."""

    state: HealthState
    queued: int
    in_flight: int = Field(
        description="The batch the writer holds: being collected (so non-zero while "
        "writer_state is IDLE), being written, or kept after a permanent error"
    )
    retry_pending: int
    total_uncommitted: int
    retry_count: int
    oldest_pending_age_s: float | None
    last_commit_at: datetime | None
    writer_state: WriterState
    active_connections: int
