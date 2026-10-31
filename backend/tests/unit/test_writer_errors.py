import asyncmy.errors
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.ingestion.writer import is_transient


def server_error(errno: int) -> DBAPIError:
    """What SQLAlchemy raises for an ERR packet with this errno from MariaDB through asyncmy."""
    try:
        asyncmy.errors.raise_mysql_exception(b"\xff" + errno.to_bytes(2, "little") + b"#HY000x")
    except asyncmy.errors.Error as orig:
        return DBAPIError.instance("INSERT INTO traffic_records", {}, orig, asyncmy.errors.Error)
    raise AssertionError("raise_mysql_exception did not raise")


@pytest.mark.parametrize(
    "errno",
    [
        4025,  # CHECK constraint failed
        1054,  # unknown column
        1292,  # incorrect datetime value
        1452,  # foreign key violation
        1406,  # data too long
    ],
)
def test_a_bad_batch_is_permanent_even_when_asyncmy_calls_it_operational(errno: int) -> None:
    assert not is_transient(server_error(errno))


@pytest.mark.parametrize(
    "errno",
    [
        2013,  # lost connection during query
        2003,  # can't connect
        1213,  # deadlock
        1205,  # lock wait timeout
        1062,  # duplicate key on the missing-only insert
    ],
)
def test_connection_loss_and_lock_conflicts_are_transient(errno: int) -> None:
    assert is_transient(server_error(errno))


def test_an_invalidated_connection_is_transient_whatever_the_error_class() -> None:
    orig = asyncmy.errors.InternalError(0, "")
    exc = DBAPIError.instance("INSERT", {}, orig, asyncmy.errors.Error, connection_invalidated=True)

    assert is_transient(exc)


def test_attempt_and_pool_timeouts_are_transient() -> None:
    assert is_transient(TimeoutError())
    assert is_transient(PoolTimeoutError("QueuePool limit reached"))
    assert not is_transient(ValueError("not a database error"))
