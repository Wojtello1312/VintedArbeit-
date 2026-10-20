"""Whitelist of editable business settings. No credentials or filesystem paths."""

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from app.admin.collection import CollectionPolicy
from app.ai.config import AIConfig
from app.deal_config import DealConfig
from app.schemas.pricing import FrozenSchema
from vinted_deal_hunter.config import Settings


class PanelPolicy(FrozenSchema):
    watched_brands: tuple[str, ...] = Field(max_length=100)
    min_price: Decimal = Field(ge=0, le=1000000000)
    max_price: Decimal = Field(ge=0, le=1000000000)
    min_score: Decimal = Field(ge=0, le=100)
    min_profit: Decimal = Field(ge=0, le=1000000000)
    scheduler_enabled: bool
    interval_seconds: int = Field(ge=1, le=86400)
    ai_enabled: bool
    ai_daily_budget_usd: Decimal = Field(ge=0, le=1000000)
    collection: CollectionPolicy | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.min_price > self.max_price:
            raise ValueError("Cena minimalna nie może przekraczać maksymalnej.")
        normalized = DealConfig(allowed_brands=self.watched_brands).allowed_brands
        if any(len(value) > 200 for value in normalized):
            raise ValueError("Nazwa marki jest zbyt długa.")
        object.__setattr__(self, "watched_brands", tuple(dict.fromkeys(normalized)))
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> "PanelPolicy":
        return cls(
            watched_brands=settings.deal.allowed_brands,
            min_price=settings.deal.min_price,
            max_price=settings.deal.max_price,
            min_score=settings.deal.approval_score,
            min_profit=settings.deal.min_profit,
            scheduler_enabled=settings.scheduler.enabled,
            interval_seconds=settings.scheduler.interval_seconds,
            ai_enabled=settings.ai.enabled,
            ai_daily_budget_usd=settings.ai.daily_budget_usd,
        )

    def apply(self, settings: Settings) -> Settings:
        deal = DealConfig.model_validate(
            {
                **settings.deal.model_dump(),
                "allowed_brands": self.watched_brands,
                "min_price": self.min_price,
                "max_price": self.max_price,
                "approval_score": self.min_score,
                "min_profit": self.min_profit,
                "ai_review_required": self.ai_enabled,
            }
        )
        ai = AIConfig.model_validate(
            {
                **settings.ai.model_dump(),
                "enabled": self.ai_enabled,
                "daily_budget_usd": self.ai_daily_budget_usd,
            }
        )
        scheduler = settings.scheduler.model_copy(
            update={"enabled": self.scheduler_enabled, "interval_seconds": self.interval_seconds}
        )
        vinted = self.collection.apply(settings.vinted) if self.collection else settings.vinted
        return settings.model_copy(
            update={"deal": deal, "ai": ai, "scheduler": scheduler, "vinted": vinted}
        )
