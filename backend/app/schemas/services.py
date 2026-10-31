from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas import NonNegativeInt


class ApiServiceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    slug: str
    display_name: str
    alert_threshold: int
    is_active: bool
    updated_at: datetime


class ApiServiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_threshold: NonNegativeInt
