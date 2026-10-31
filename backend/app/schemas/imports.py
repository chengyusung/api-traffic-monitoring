from datetime import datetime
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from app.models import ImportStatus


class RowError(TypedDict):
    """One reason a file was rejected.

    `row` is the CSV physical line where the record ends (the header is line 1; a quoted field
    with line breaks spans several) or the 1-based position in the JSON array. It is null when
    the problem is the whole file (size, type, encoding, header, no data rows).
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
