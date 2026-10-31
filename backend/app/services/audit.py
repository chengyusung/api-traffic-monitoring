from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_var
from app.models import AuditEvent, SystemLog


def record_event(
    session: AsyncSession,
    event_type: AuditEvent,
    message: str,
    *,
    level: str = "INFO",
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the caller's transaction; it commits or rolls back with the change."""
    session.add(
        SystemLog(
            level=level,
            event_type=event_type,
            actor_id=actor_id,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            message=message,
            context=context,
            request_id=request_id_var.get(),
        )
    )
