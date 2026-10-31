from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.errors import NotFoundError
from app.models import Role, TrafficRecord, User
from app.schemas.records import RecordCreate, RecordUpdate
from app.services.records import assert_can_modify, is_anomaly

VALID = {
    "service_slug": "payment",
    "title": "checkout burst",
    "value": 81,
    "observed_at": "2026-10-31T16:00:04+08:00",
}


@pytest.mark.parametrize(("value", "expected"), [(79, False), (80, False), (81, True)])
def test_anomaly_is_strictly_greater_than_threshold(value: int, expected: bool) -> None:
    assert is_anomaly(value, 80) is expected


@pytest.mark.parametrize("value", [0, 2**31 - 1])
def test_value_accepts_non_negative_integers(value: int) -> None:
    assert RecordCreate.model_validate({**VALID, "value": value}).value == value


@pytest.mark.parametrize("value", [-1, 1.5, 5.0, "5", True, None, 2**31])
def test_value_rejects_anything_but_a_non_negative_int(value: Any) -> None:
    with pytest.raises(ValidationError):
        RecordCreate.model_validate({**VALID, "value": value})


def test_naive_observed_at_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        RecordCreate.model_validate({**VALID, "observed_at": "2026-10-31T16:00:04"})


def test_observed_at_is_converted_to_utc() -> None:
    observed_at = RecordCreate.model_validate(VALID).observed_at

    assert observed_at == datetime(2026, 10, 31, 16, 0, 4, tzinfo=timezone(timedelta(hours=8)))
    assert observed_at.tzinfo is UTC


def test_update_rejects_explicit_null() -> None:
    with pytest.raises(ValidationError, match="cannot be null: value"):
        RecordUpdate.model_validate({"value": None})


ACTOR_ID, ADMIN_ID, OTHER_USER_ID = 10, 1, 2
OWNERS = {"owner": ACTOR_ID, "admin": ADMIN_ID, "other user": OTHER_USER_ID, "NULL": None}
# ADR-002: ADMIN modifies anything; USER only rows it created; VIEWER nothing.
ALLOWED = {
    Role.ADMIN: {"owner", "admin", "other user", "NULL"},
    Role.USER: {"owner"},
    Role.VIEWER: set(),
}


@pytest.mark.parametrize("owner", OWNERS)
@pytest.mark.parametrize("role", list(Role))
def test_ownership_table(owner: str, role: Role) -> None:
    record = TrafficRecord(created_by=OWNERS[owner])
    actor = User(id=ACTOR_ID, role=role)

    if owner in ALLOWED[role]:
        assert_can_modify(record, actor)
    else:
        with pytest.raises(NotFoundError):
            assert_can_modify(record, actor)
