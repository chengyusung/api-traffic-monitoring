from typing import Annotated

from pydantic import Field

# Strict: a JSON true, 5.0 or "5" is not a count. Upper bound is MariaDB's signed INT.
NonNegativeInt = Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)]
