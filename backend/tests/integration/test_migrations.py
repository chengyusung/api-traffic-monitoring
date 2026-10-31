import asyncio
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import make_engine

APP_TABLES = {"users", "api_services", "traffic_records", "import_batches", "system_logs"}


def table_names() -> set[str]:
    async def read() -> set[str]:
        engine = make_engine(os.environ["TEST_DATABASE_URL"])
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        await engine.dispose()
        return set(names)

    return asyncio.run(read())


def test_downgrade_to_base_then_upgrade_to_head(alembic_config: Config, migrated: None) -> None:
    command.downgrade(alembic_config, "base")
    assert table_names() == {"alembic_version"}

    command.upgrade(alembic_config, "head")
    assert table_names() == APP_TABLES | {"alembic_version"}
    # Fails if the migration and the models have drifted apart.
    command.check(alembic_config)
