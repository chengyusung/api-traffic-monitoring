import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, UnprocessableError
from app.db import flush_or_conflict
from app.models import RecordSource, Role, TrafficRecord, User, utcnow
from app.repositories import api_services, records
from app.schemas.records import RecordCreate, RecordListQuery, RecordPage, RecordRead, RecordUpdate
from app.services import audit

NOT_FOUND = "Record not found."


def is_anomaly(value: int, threshold: int) -> bool:
    return value > threshold


def assert_can_modify(record: TrafficRecord, user: User) -> None:
    """Raise NotFoundError, not 403, so a refusal never confirms that the record exists."""
    if not (user.role == Role.ADMIN or (user.role == Role.USER and record.created_by == user.id)):
        raise NotFoundError(NOT_FOUND)


async def get_record(session: AsyncSession, record_id: int) -> TrafficRecord:
    record = await records.get(session, record_id)
    if record is None:
        raise NotFoundError(NOT_FOUND)
    return record


async def list_records(session: AsyncSession, query: RecordListQuery) -> RecordPage:
    rows, total = await records.list_page(session, query)
    return RecordPage(
        items=[RecordRead.model_validate(row) for row in rows],
        page=query.page,
        page_size=query.page_size,
        total=total,
    )


async def create_records(
    session: AsyncSession, items: Sequence[RecordCreate], actor: User
) -> list[TrafficRecord]:
    """Insert every item in one transaction, or none of them."""
    services = await api_services.get_by_slugs(session, (item.service_slug for item in items))
    if unknown := sorted({item.service_slug for item in items} - services.keys()):
        raise UnprocessableError(f"Unknown service_slug: {', '.join(unknown)}")
    persisted_at = utcnow()
    created = [
        TrafficRecord(
            event_id=str(item.event_id or uuid.uuid4()),
            service_id=services[item.service_slug].id,
            title=item.title,
            value=item.value,
            source=RecordSource.MANUAL,
            created_by=actor.id,
            observed_at=item.observed_at,
            persisted_at=persisted_at,
            threshold_applied=services[item.service_slug].alert_threshold,
            is_anomaly=is_anomaly(item.value, services[item.service_slug].alert_threshold),
        )
        for item in items
    ]
    session.add_all(created)
    await flush_or_conflict(session, "A record with this event_id already exists.")
    await session.commit()
    return created


async def update_record(
    session: AsyncSession, record_id: int, changes: RecordUpdate, actor: User
) -> TrafficRecord:
    record = await get_record(session, record_id)
    assert_can_modify(record, actor)
    fields = changes.model_dump(exclude_unset=True)
    before = {"value": record.value, "is_anomaly": record.is_anomaly}
    for name, value in fields.items():
        setattr(record, name, value)
    if "value" in fields:
        # The record's own snapshot, never the service's current threshold.
        record.is_anomaly = is_anomaly(record.value, record.threshold_applied)
        audit.record_event(
            session,
            "record.value_updated",
            f"record {record.id} value {before['value']} -> {record.value}",
            actor_id=actor.id,
            target_type="traffic_record",
            target_id=record.id,
            context={
                "before": before,
                "after": {"value": record.value, "is_anomaly": record.is_anomaly},
            },
        )
    await session.commit()
    return record


async def delete_record(session: AsyncSession, record_id: int, actor: User) -> None:
    record = await get_record(session, record_id)
    assert_can_modify(record, actor)
    audit.record_event(
        session,
        "record.deleted",
        f"record {record.id} deleted",
        actor_id=actor.id,
        target_type="traffic_record",
        target_id=record.id,
        context={
            "event_id": record.event_id,
            "service_id": record.service_id,
            "value": record.value,
            "created_by": record.created_by,
        },
    )
    await session.delete(record)
    await session.commit()
