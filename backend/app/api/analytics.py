from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, Session
from app.schemas.analytics import Aggregate, ServiceAggregate, TrendPoint, TrendQuery
from app.schemas.records import RecordFilters
from app.services import analytics

router = APIRouter(prefix="/analytics", tags=["analytics"])

# The records list's filters, so a list total and an analytics count always agree.
Filters = Annotated[RecordFilters, Query()]


@router.get("/summary")
async def summary(filters: Filters, _: CurrentUser, session: Session) -> Aggregate:
    return await analytics.summary(session, filters)


@router.get("/categories")
async def categories(filters: Filters, _: CurrentUser, session: Session) -> list[ServiceAggregate]:
    return await analytics.categories(session, filters)


@router.get("/trends")
async def trends(
    query: Annotated[TrendQuery, Query()], _: CurrentUser, session: Session
) -> list[TrendPoint]:
    return await analytics.trends(session, query)
