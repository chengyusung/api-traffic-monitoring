from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    column,
    or_,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models.types import UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


def non_negative(name: str) -> CheckConstraint:
    return CheckConstraint(column(name) >= 0, name=f"{name}_non_negative")


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )
    type_annotation_map = {datetime: UTCDateTime(), dict[str, Any]: JSON, list[Any]: JSON}


class Role(StrEnum):
    ADMIN = "ADMIN"
    USER = "USER"
    VIEWER = "VIEWER"


class RecordSource(StrEnum):
    MANUAL = "MANUAL"
    IMPORT = "IMPORT"
    SIMULATOR = "SIMULATOR"


class ImportStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AuditEvent(StrEnum):
    """Every system_logs.event_type the application writes; GET /admin/logs filters on these."""

    AUTH_LOGIN = "auth.login"
    AUTH_REGISTER = "auth.register"
    USER_UPDATED = "user.updated"
    RECORD_UPDATED = "record.updated"
    RECORD_DELETED = "record.deleted"
    SERVICE_THRESHOLD_UPDATED = "service.threshold_updated"
    IMPORT_SUCCEEDED = "import.succeeded"
    IMPORT_FAILED = "import.failed"
    WRITER_RECOVERED = "writer.recovered"
    WRITER_FAILED = "writer.failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApiService(Base):
    __tablename__ = "api_services"
    __table_args__ = (non_negative("alert_threshold"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))
    alert_threshold: Mapped[int]
    is_active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        non_negative("total_count"),
        non_negative("failure_count"),
        # All-or-Nothing: a batch writes every row or none of them.
        CheckConstraint(
            or_(
                column("success_count") == 0,
                column("success_count") == column("total_count"),
            ),
            name="success_count_all_or_nothing",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[ImportStatus] = mapped_column(Enum(ImportStatus, name="import_status"))
    total_count: Mapped[int]
    success_count: Mapped[int]
    failure_count: Mapped[int]
    errors: Mapped[list[Any] | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TrafficRecord(Base):
    __tablename__ = "traffic_records"
    __table_args__ = (
        non_negative("value"),
        non_negative("threshold_applied"),
        Index("ix_traffic_records_service_id_observed_at", "service_id", "observed_at"),
        Index("ix_traffic_records_source_observed_at", "source", "observed_at"),
        Index("ix_traffic_records_created_by", "created_by"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(CHAR(36), unique=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("api_services.id"))
    title: Mapped[str] = mapped_column(String(255))
    value: Mapped[int]
    source: Mapped[RecordSource] = mapped_column(Enum(RecordSource, name="record_source"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    observed_at: Mapped[datetime]
    persisted_at: Mapped[datetime]
    threshold_applied: Mapped[int]
    is_anomaly: Mapped[bool]
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    level: Mapped[str] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any] | None]
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
