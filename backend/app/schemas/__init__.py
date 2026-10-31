from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Strict: a JSON true, 5.0 or "5" is not a count. Upper bound is MariaDB's signed INT.
NonNegativeInt = Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)]


class PartialUpdate(BaseModel):
    """A PATCH body: at least one field, and none of them null."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reject_empty_and_nulls(self) -> Self:
        if not self.model_fields_set:
            raise ValueError(f"send at least one of {', '.join(type(self).model_fields)}")
        if nulls := sorted(name for name in self.model_fields_set if getattr(self, name) is None):
            raise ValueError(f"cannot be null: {', '.join(nulls)}")
        return self


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int = Field(default=20, ge=1, le=100)
