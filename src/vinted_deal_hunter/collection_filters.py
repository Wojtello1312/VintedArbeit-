"""Explicit collection criteria, independent of valuation and scoring."""

import re
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vinted_deal_hunter.models import Condition, OfferInput, clean_text


class CollectionFilters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    currency: str = "PLN"
    min_price: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    max_price: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    condition: Condition | None = None
    category: str = Field(default="", max_length=200)
    brand: str = Field(default="", max_length=200)
    size: str = Field(default="", max_length=100)

    @field_validator("currency", mode="before")
    @classmethod
    def valid_currency(cls, value: object) -> str:
        return OfferInput.normalize_currency(value)

    @field_validator("min_price", "max_price", "condition", mode="before")
    @classmethod
    def optional_value(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("category", "brand", "size")
    @classmethod
    def normalized(cls, value: str) -> str:
        return clean_text(value)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.min_price is not None and self.max_price is not None:
            if self.min_price > self.max_price:
                raise ValueError("Cena od nie może być większa niż cena do")
        return self

    def rejection(self, offer: OfferInput) -> str | None:
        if offer.currency != self.currency:
            return "currency_mismatch"
        if self.min_price is not None and offer.asking_price < self.min_price:
            return "price_below_minimum"
        if self.max_price is not None and offer.asking_price > self.max_price:
            return "price_above_maximum"
        if self.condition is not None and (offer.condition or Condition.UNKNOWN) != self.condition:
            return "condition_unknown_or_mismatch"
        if self.brand and self.brand.casefold() != (offer.brand or "").casefold():
            return "brand_unknown_or_mismatch"
        if self.size and self.size.casefold() != (offer.size or "").casefold():
            return "size_unknown_or_mismatch"
        categories = re.split(r"\s*(?:/|>|»)\s*", (offer.category or "").casefold())
        if self.category and self.category.casefold() not in categories:
            return "category_unknown_or_mismatch"
        return None

    def search_parameters(self) -> dict[str, str]:
        """Only public catalog query parameters; verify each imported offer again."""
        params = {"currency": self.currency}
        if self.min_price is not None:
            params["price_from"] = str(self.min_price)
        if self.max_price is not None:
            params["price_to"] = str(self.max_price)
        statuses = {
            Condition.NEW_WITH_TAGS: "6",
            Condition.NEW_WITHOUT_TAGS: "1",
            Condition.VERY_GOOD: "2",
            Condition.GOOD: "3",
            Condition.FAIR: "4",
        }
        if self.condition in statuses:
            params["status_ids[]"] = statuses[self.condition]
        return params
