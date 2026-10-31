from fastapi import APIRouter

from app.api.deps import Admin, CurrentUser, Session
from app.schemas.services import ApiServiceRead, ApiServiceUpdate
from app.services import api_services

router = APIRouter(prefix="/services", tags=["services"])


@router.get("")
async def list_services(_: CurrentUser, session: Session) -> list[ApiServiceRead]:
    return [ApiServiceRead.model_validate(s) for s in await api_services.list_services(session)]


@router.patch("/{service_id}")
async def update_service(
    service_id: int, data: ApiServiceUpdate, admin: Admin, session: Session
) -> ApiServiceRead:
    service = await api_services.update_threshold(session, service_id, data.alert_threshold, admin)
    return ApiServiceRead.model_validate(service)
