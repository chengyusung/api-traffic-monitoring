import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.models import RecordSource
from app.schemas import NonNegativeInt, Pagination, PartialUpdate

# No C0 control character: openpyxl refuses most of them, so one stored title would break every
# export that covers its row.
Title = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f]*$")]


def _to_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("datetime has no UTC equivalent") from exc


# Naive input is a 422; aware input is converted to UTC at the boundary.
UTCInstant = Annotated[AwareDatetime, AfterValidator(_to_utc)]
# The only sort allowlist: each key is a TrafficRecord column; "-" means descending.
RecordSort = Literal[
    "observed_at", "-observed_at", "value", "-value", "persisted_at", "-persisted_at"
]


class RecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID | None = None
    service_slug: str = Field(max_length=32)
    title: Title
    value: NonNegativeInt
    observed_at: UTCInstant


class RecordUpdate(PartialUpdate):
    title: Title | None = None
    value: NonNegativeInt | None = None
    observed_at: UTCInstant | None = None


class RecordRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    event_id: str
    service_id: int
    title: str
    value: int
    source: RecordSource
    created_by: int | None
    observed_at: datetime
    persisted_at: datetime
    threshold_applied: int
    is_anomaly: bool
    import_batch_id: int | None


class RecordFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str | None = None
    source: RecordSource | None = None
    is_anomaly: bool | None = None
    start: UTCInstant | None = Field(default=None, description="Inclusive: observed_at >= start")
    end: UTCInstant | None = Field(default=None, description="Exclusive: observed_at < end")
    q: str | None = Field(default=None, max_length=255, description="Substring of title")

    @model_validator(mode="after")
    def reject_reversed_window(self) -> Self:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class RecordListQuery(RecordFilters, Pagination):
    sort: RecordSort = "-observed_at"


class RecordPage(BaseModel):
    items: list[RecordRead]
    page: int
    page_size: int
    total: int
