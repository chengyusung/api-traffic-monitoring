from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.ingestion.buffer import WriterState
from app.models import AuditEvent, AuditLevel, Role
from app.schemas import Pagination, PartialUpdate
from app.schemas.auth import UserRead
from app.schemas.realtime import Readiness
from app.schemas.records import UTCInstant


class UserPage(BaseModel):
    items: list[UserRead]
    page: int
    page_size: int
    total: int


class UserUpdate(PartialUpdate):
    """ADR-002: an admin changes role and is_active only; users are never deleted."""

    role: Role | None = None
    is_active: StrictBool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_email(cls, data: Any) -> Any:
        # A reason instead of the generic extra-field error: email is immutable by rule.
        if isinstance(data, dict) and "email" in data:
            raise ValueError("email cannot be changed")
        return data


class LogQuery(Pagination):
    level: AuditLevel | None = None
    event_type: AuditEvent | None = None
    start: UTCInstant | None = Field(default=None, description="Inclusive: created_at >= start")
    end: UTCInstant | None = Field(default=None, description="Exclusive: created_at < end")

    @model_validator(mode="after")
    def reject_reversed_window(self) -> Self:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class LogRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    level: str
    event_type: str
    actor_id: int | None
    target_type: str | None
    target_id: str | None
    message: str
    context: dict[str, Any] | None
    request_id: str | None
    created_at: datetime


class LogPage(BaseModel):
    """Newest first; id breaks ties between rows with the same created_at."""

    items: list[LogRead]
    page: int
    page_size: int
    total: int


class PoolStatus(BaseModel):
    status: str = Field(description="engine.pool.status() as SQLAlchemy prints it")
    pool_size: int
    max_overflow: int


class GeneratorStatus(BaseModel):
    thresholds: dict[str, int] | None = Field(
        description="slug -> alert_threshold as the simulator last loaded it; null before the "
        "first load"
    )
    thresholds_loaded_at: datetime | None
    paused_s: float = Field(description="Seconds the simulator waited on a full buffer")
    stopped_by: WriterState | None = Field(
        description="The halted writer state that stopped the simulator; null while it runs"
    )


class SystemStatus(BaseModel):
    readiness: Readiness
    pool: PoolStatus
    generator: GeneratorStatus
    table_counts: dict[str, int] = Field(
        description="Rows per table, approximate within 10 s: the counts are cached in-process"
    )
