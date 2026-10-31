"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-31 23:47:22.934404

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def non_negative(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sa.column(column) >= 0, name=f"ck_{table}_{column}_non_negative")


def upgrade() -> None:
    op.create_table(
        "api_services",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("alert_threshold", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        non_negative("api_services", "alert_threshold"),
        sa.UniqueConstraint("slug", name="uq_api_services_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.Enum("ADMIN", "USER", "VIEWER", name="role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Enum("SUCCEEDED", "FAILED", name="import_status"), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        non_negative("import_batches", "total_count"),
        non_negative("import_batches", "failure_count"),
        sa.CheckConstraint(
            sa.or_(
                sa.column("success_count") == 0,
                sa.column("success_count") == sa.column("total_count"),
            ),
            name="ck_import_batches_success_count_all_or_nothing",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"], name="fk_import_batches_uploaded_by_users"
        ),
    )
    op.create_table(
        "system_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_system_logs_actor_id_users"),
    )
    op.create_table(
        "traffic_records",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", sa.CHAR(length=36), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum("MANUAL", "IMPORT", "SIMULATOR", name="record_source"),
            nullable=False,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("observed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("persisted_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("threshold_applied", sa.Integer(), nullable=False),
        sa.Column("is_anomaly", sa.Boolean(), nullable=False),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=True),
        non_negative("traffic_records", "value"),
        non_negative("traffic_records", "threshold_applied"),
        sa.ForeignKeyConstraint(
            ["service_id"], ["api_services.id"], name="fk_traffic_records_service_id_api_services"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_traffic_records_created_by_users"
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batches.id"],
            name="fk_traffic_records_import_batch_id_import_batches",
        ),
        sa.UniqueConstraint("event_id", name="uq_traffic_records_event_id"),
    )
    op.create_index(
        "ix_traffic_records_service_id_observed_at",
        "traffic_records",
        ["service_id", "observed_at"],
    )
    op.create_index(
        "ix_traffic_records_source_observed_at", "traffic_records", ["source", "observed_at"]
    )
    op.create_index("ix_traffic_records_created_by", "traffic_records", ["created_by"])


def downgrade() -> None:
    # Indexes go with their tables; MariaDB refuses to drop an index a foreign key still uses.
    op.drop_table("traffic_records")
    op.drop_table("system_logs")
    op.drop_table("import_batches")
    op.drop_table("users")
    op.drop_table("api_services")
