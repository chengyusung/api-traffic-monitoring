from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models import Role

Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    ),
]


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    password: str = Field(min_length=8)


class UserRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    email: str
    role: Role
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
