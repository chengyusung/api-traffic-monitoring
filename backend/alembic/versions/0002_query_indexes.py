"""query indexes for logs, time windows and the admin lock

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01 06:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = (
    ("ix_system_logs_created_at_id", "system_logs", ["created_at", "id"]),
    ("ix_system_logs_event_type_created_at", "system_logs", ["event_type", "created_at"]),
    ("ix_traffic_records_observed_at", "traffic_records", ["observed_at"]),
    ("ix_users_role_is_active", "users", ["role", "is_active"]),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _ in INDEXES:
        op.drop_index(name, table_name=table)
