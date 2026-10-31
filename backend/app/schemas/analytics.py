from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.records import RecordFilters

# architecture.md §7: a mixed-service average is not overall RPS.
AVG_DESCRIPTION = (
    "Mean value per service-second observation. For one service this is its average RPS; "
    "across services it is not overall RPS."
)


class Aggregate(BaseModel):
    """Null sum / avg / min / max mean no matching rows, never zero."""

    count: int
    sum: int | None
    avg_per_observation: float | None = Field(description=AVG_DESCRIPTION)
    min: int | None
    max: int | None
    anomaly_count: int


class ServiceAggregate(Aggregate):
    service: str


class TrendPoint(Aggregate):
    bucket_start: datetime


class TrendQuery(RecordFilters):
    bucket: Literal["minute", "hour"] = "minute"
