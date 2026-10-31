"""WebSocket wire schema (ADR-003). Pushed before commit, so no id and no persisted_at."""

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
