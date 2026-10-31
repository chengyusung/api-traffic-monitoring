from datetime import datetime
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from app.models import ImportStatus


class RowError(TypedDict):
    """One reason a file was rejected.

    `row` is the CSV line number (the header is line 1) or the 1-based position in the JSON
    array; it is null when the problem is the whole file (size, type, encoding, header).
    """

    row: int | None
    field: str | None
    message: str


class ImportBatchRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    filename: str
    uploaded_by: int
    status: ImportStatus
    total_count: int
    success_count: int
    failure_count: int
    errors: list[RowError] | None
    created_at: datetime
