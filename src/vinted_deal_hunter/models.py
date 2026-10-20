"""Normalized input contracts. Unknown metadata remains explicitly absent."""

import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Only these currencies are supported in this ingestion release; no inferred currency.
CURRENCY_SCALE = {
    "PLN": 2,
    "EUR": 2,
    "USD": 2,
    "GBP": 2,
    "CHF": 2,
    "CZK": 2,
    "SEK": 2,
    "DKK": 2,
    "NOK": 2,
    "JPY": 0,
}


def clean_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


class Condition(StrEnum):
    NEW_WITH_TAGS = "new_with_tags"
    NEW_WITHOUT_TAGS = "new_without_tags"
    VERY_GOOD = "very_good"
    GOOD = "good"
    FAIR = "fair"
    UNKNOWN = "unknown"


CONDITION_ALIASES = {
    "nowy z metką": Condition.NEW_WITH_TAGS,
    "nowy bez metki": Condition.NEW_WITHOUT_TAGS,
    "bardzo dobry": Condition.VERY_GOOD,
    "dobry": Condition.GOOD,
    "zadowalający": Condition.FAIR,
    "nieznany": Condition.UNKNOWN,
}


class SourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_name: Annotated[
        str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    ]
    is_demo: bool = Field(strict=True)

    @field_validator("source_name", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> object:
        return clean_text(value).casefold() if isinstance(value, str) else value


class OfferInput(SourceIdentity):
    external_id: Annotated[str, Field(min_length=1, max_length=200)]
    product_name: Annotated[str, Field(min_length=1, max_length=500)]
    brand: Annotated[str | None, Field(max_length=200)] = None
    category: Annotated[str | None, Field(max_length=200)] = None
    size: Annotated[str | None, Field(max_length=100)] = None
    condition: Condition | None = None
    currency: str
    asking_price: Decimal
    published_at: datetime | None = None

    @field_validator("external_id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("external_id must be a string or integer")
        # Preserve case, inner spaces and leading zeroes: identifiers are opaque.
        result = unicodedata.normalize("NFC", str(value)).strip()
        if any(unicodedata.category(c).startswith("C") for c in result):
            raise ValueError("external_id contains control characters")
        return result

    @field_validator("product_name", "brand", "size", "category", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return clean_text(value) if isinstance(value, str) else value

    @field_validator("brand", "size", "category", mode="before")
    @classmethod
    def empty_metadata(cls, value: object) -> object:
        if isinstance(value, str) and not clean_text(value):
            return None
        return value

    @field_validator("brand")
    @classmethod
    def normalize_brand(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.casefold()
        return {"h & m": "h&m", "h and m": "h&m"}.get(normalized, normalized)

    @field_validator("size")
    @classmethod
    def normalize_size(cls, value: str | None) -> str | None:
        # Do not infer equivalence between size systems (EU 38 is not assumed M).
        return value.upper() if value else None

    @field_validator("condition", mode="before")
    @classmethod
    def normalize_condition(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            normalized = clean_text(value).casefold()
            if not normalized:
                return None
            return CONDITION_ALIASES.get(normalized, normalized)
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.strip().upper() not in CURRENCY_SCALE:
            raise ValueError("currency must be an explicitly supported ISO currency code")
        return value.strip().upper()

    @field_validator("asking_price", mode="before")
    @classmethod
    def normalize_price(cls, value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("price must be an exact decimal string or JSON number")
        if isinstance(value, str):
            value = value.strip()
            if not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", value):
                raise ValueError("price requires digits and an optional decimal separator")
            value = value.replace(",", ".")
        try:
            price = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal price") from exc
        if not price.is_finite() or price <= 0 or price > Decimal("1000000000"):
            raise ValueError("price must be finite, positive and at most 1000000000")
        return price

    @field_validator("published_at", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.strip())
            except ValueError as exc:
                raise ValueError(
                    "published_at must be an ISO-8601 timestamp with timezone"
                ) from exc
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at requires an explicit timezone; no inferred UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def check_price_scale(self) -> Self:
        quantum = Decimal(1).scaleb(-CURRENCY_SCALE[self.currency])
        try:
            normalized = self.asking_price.quantize(quantum)
        except InvalidOperation as exc:
            raise ValueError("invalid precision for currency") from exc
        if normalized != self.asking_price:
            raise ValueError("price has more fractional digits than currency allows")
        object.__setattr__(self, "asking_price", normalized)
        return self

    @property
    def price_minor(self) -> int:
        return int(self.asking_price * 10 ** CURRENCY_SCALE[self.currency])

    @property
    def listing_url(self) -> str | None:
        if (
            not self.is_demo
            and self.source_name in {"vinted-public-pl", "vinted-manual"}
            and re.fullmatch(r"[0-9]+", self.external_id)
        ):
            return f"https://www.vinted.pl/items/{self.external_id}"
        return None
