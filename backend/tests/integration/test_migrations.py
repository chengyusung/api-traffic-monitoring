import asyncio
import os
from collections.abc import Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import Inspector, inspect

from app.db import make_engine

APP_TABLES = {"users", "api_services", "traffic_records", "import_batches", "system_logs"}
# 0002: table -> the indexes it adds.
QUERY_INDEXES = {
    "system_logs": {"ix_system_logs_created_at_id", "ix_system_logs_event_type_created_at"},
    "traffic_records": {"ix_traffic_records_observed_at"},
    "users": {"ix_users_role_is_active"},
}


def inspected[T](read: Callable[[Inspector], T]) -> T:
    async def run() -> T:
        engine = make_engine(os.environ["TEST_DATABASE_URL"])
        async with engine.connect() as conn:
            result = await conn.run_sync(lambda sync_conn: read(inspect(sync_conn)))
        await engine.dispose()
        return result

    return asyncio.run(run())


def table_names() -> set[str]:
    return set(inspected(lambda inspector: inspector.get_table_names()))


def query_indexes() -> dict[str, set[str]]:
    return inspected(
        lambda inspector: {
            table: {str(ix["name"]) for ix in inspector.get_indexes(table)} & names
            for table, names in QUERY_INDEXES.items()
        }
    )


def test_downgrade_to_base_then_upgrade_to_head(alembic_config: Config, migrated: None) -> None:
    command.downgrade(alembic_config, "base")
    assert table_names() == {"alembic_version"}

    command.upgrade(alembic_config, "head")
    assert table_names() == APP_TABLES | {"alembic_version"}
    # Fails if the migration and the models have drifted apart.
    command.check(alembic_config)


def test_0002_adds_the_query_indexes_and_its_downgrade_drops_them(
    alembic_config: Config, migrated: None
) -> None:
    command.downgrade(alembic_config, "0001")
    without = query_indexes()
    command.upgrade(alembic_config, "head")

    assert without == {table: set() for table in QUERY_INDEXES}
    assert query_indexes() == QUERY_INDEXES
