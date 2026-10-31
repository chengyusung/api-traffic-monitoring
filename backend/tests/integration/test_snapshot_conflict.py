import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import database_unavailable, driver_errno
from app.models import TrafficRecord
from tests.api.test_records import insert_record


async def test_updating_a_row_changed_since_the_snapshot_is_a_retryable_1020(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> None:
    """The shape of every read-then-write request (PATCH /records, /services, /admin/users):
    MariaDB 11.7 aborts it with 1020 when another commit changed the row in between."""
    record_id = await insert_record(session_factory, services["auth"], None)
    async with session_factory() as first, session_factory() as second:
        mine = await first.get(TrafficRecord, record_id)
        theirs = await second.get(TrafficRecord, record_id)
        assert mine is not None and theirs is not None
        theirs.value = 1
        await second.commit()

        mine.value = 2
        with pytest.raises(OperationalError) as caught:
            await first.commit()

    assert driver_errno(caught.value) == 1020
    assert database_unavailable(caught.value), "a retry can succeed, so REST answers 503"
