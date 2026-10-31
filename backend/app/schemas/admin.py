from typing import Any

from pydantic import BaseModel, StrictBool, model_validator

from app.models import Role
from app.schemas import PartialUpdate
from app.schemas.auth import UserRead


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
