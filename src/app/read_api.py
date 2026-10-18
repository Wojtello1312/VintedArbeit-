"""Authenticated, paginated REST access to persisted offers and latest analyses."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.admin.queries import AdminQueries, OfferFilters
from app.schemas.deals import AnalysisRecord
from app.security import bearer_auth
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.models import OfferInput


class OfferResult(BaseModel):
    key: str
    offer: OfferInput
    state: str
    score: Decimal | None
    estimated_profit: Decimal | None
    risk_level: str | None
    feedback: str | None


class OfferPage(BaseModel):
    items: list[OfferResult]
    total: int
    page: int
    page_size: int = 25


class OfferDetail(BaseModel):
    key: str
    offer: OfferInput
    analysis: AnalysisRecord | None
    feedback: str | None


def read_router(settings: Settings) -> APIRouter:
    router = APIRouter(
        tags=["offers"], dependencies=[Depends(bearer_auth(settings.jobs_api_token))]
    )

    @router.get("/offers", response_model=OfferPage)
    def offers(filters: Annotated[OfferFilters, Query()]) -> OfferPage:
        rows, total = AdminQueries(settings).offers(filters)
        items = []
        for row in rows:
            record = row.analysis
            items.append(
                OfferResult(
                    key=row.key,
                    offer=row.offer,
                    state=record.state if record else "NEW",
                    score=record.score.total if record and record.score else None,
                    estimated_profit=record.profit.estimated_profit
                    if record and record.profit
                    else None,
                    risk_level=record.risk.level if record and record.risk else None,
                    feedback=row.label,
                )
            )
        return OfferPage(items=items, total=total, page=filters.page)

    @router.get("/offers/{key}", response_model=OfferDetail)
    def detail(key: str) -> OfferDetail:
        row = AdminQueries(settings).offer(key) if len(key) == 64 else None
        if row is None:
            raise HTTPException(404, "Offer not found")
        return OfferDetail(key=key, offer=row.offer, analysis=row.analysis, feedback=row.label)

    return router
