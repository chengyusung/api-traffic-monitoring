from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import ApiService, AuditEvent, User
from app.repositories import api_services
from app.services import audit


async def list_services(session: AsyncSession) -> Sequence[ApiService]:
    return await api_services.list_all(session)


async def update_threshold(
    session: AsyncSession, service_id: int, threshold: int, actor: User
) -> ApiService:
    service = await api_services.get(session, service_id)
    if service is None:
        raise NotFoundError("Service not found.")
    before = service.alert_threshold
    service.alert_threshold = threshold
    audit.record_event(
        session,
        AuditEvent.SERVICE_THRESHOLD_UPDATED,
        f"{service.slug} alert_threshold {before} -> {threshold}",
        actor_id=actor.id,
        target_type="api_service",
        target_id=service.id,
        context={"before": {"alert_threshold": before}, "after": {"alert_threshold": threshold}},
    )
    await session.commit()
    return service
