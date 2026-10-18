"""Explicit evidence contracts: an asking price never implies a completed sale."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vinted_deal_hunter.models import (
    CURRENCY_SCALE,
    Condition,
    OfferInput,
    SourceIdentity,
    clean_text,
)

Nonempty = Annotated[str, Field(min_length=1, max_length=500)]


def normalized_label(value: str) -> str:
    return clean_text(value).casefold()


def normalized_name(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", normalized_label(value)).split())


def utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp requires an explicit timezone")
    return value.astimezone(UTC)


class FrozenSchema(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
    )


class ProductFeatures(FrozenSchema):
    product_name: Nonempty
    brand: Nonempty | None = None
    model: Nonempty | None = None
    size: Nonempty | None = None
    condition: Condition | None = None

    @field_validator("product_name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return normalized_name(value) if isinstance(value, str) else value

    @field_validator("brand", "model", "size", mode="before")
    @classmethod
    def normalize_optional(cls, value: object) -> object:
        return normalized_label(value) or None if isinstance(value, str) else value

    @field_validator("brand")
    @classmethod
    def normalize_brand(cls, value: str | None) -> str | None:
        return OfferInput.normalize_brand(value)

    @field_validator("condition", mode="before")
    @classmethod
    def normalize_condition(cls, value: object) -> object:
        return OfferInput.normalize_condition(value)

    @classmethod
    def from_offer(cls, offer: OfferInput, *, model: str | None = None) -> "ProductFeatures":
        return cls(
            product_name=offer.product_name,
            brand=offer.brand,
            model=model,
            size=offer.size,
            condition=offer.condition,
        )


class PricingProduct(FrozenSchema):
    product_id: Nonempty
    features: ProductFeatures
    currency: str
    is_demo: bool = Field(strict=True)
    source_name: str | None = None
    external_id: Annotated[str | None, Field(min_length=1, max_length=200)] = None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return OfferInput.normalize_currency(value)

    @field_validator("source_name", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> object:
        return OfferInput.normalize_source(value)

    @field_validator("external_id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> str | None:
        return None if value is None else OfferInput.normalize_id(value)

    @model_validator(mode="after")
    def identity_pair(self) -> Self:
        if (self.source_name is None) != (self.external_id is None):
            raise ValueError("source_name and external_id must be supplied together")
        if self.source_name is not None:
            # Reuse the ingestion identity rules, including empty identifier checks.
            SourceIdentity(source_name=self.source_name, is_demo=self.is_demo)
            if not self.external_id:
                raise ValueError("external_id must not be empty")
        return self

    @classmethod
    def from_offer(cls, offer: OfferInput, *, model: str | None = None) -> "PricingProduct":
        return cls(
            product_id=f"{offer.source_name}:{offer.external_id}",
            features=ProductFeatures.from_offer(offer, model=model),
            currency=offer.currency,
            is_demo=offer.is_demo,
            source_name=offer.source_name,
            external_id=offer.external_id,
        )


class PriceType(StrEnum):
    ASKING = "ASKING"
    SOLD = "SOLD"


class PriceObservation(FrozenSchema):
    source_name: Annotated[
        str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    ]
    external_id: Annotated[str, Field(min_length=1, max_length=200)]
    features: ProductFeatures
    price: Decimal
    currency: str
    price_type: PriceType
    observed_at: datetime
    source_reliability: float = Field(ge=0, le=1)
    is_demo: bool = Field(strict=True)
    sale_confirmed: bool = Field(default=False, strict=True)
    evidence_reference: Nonempty | None = None
    listing_disappeared: bool = Field(default=False, strict=True)

    @field_validator("source_name", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> object:
        return OfferInput.normalize_source(value)

    @field_validator("external_id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> str:
        return OfferInput.normalize_id(value)

    @field_validator("price", mode="before")
    @classmethod
    def normalize_price(cls, value: object) -> Decimal:
        return OfferInput.normalize_price(value)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return OfferInput.normalize_currency(value)

    @field_validator("observed_at")
    @classmethod
    def normalize_date(cls, value: datetime) -> datetime:
        return utc_timestamp(value)

    @field_validator("evidence_reference", mode="before")
    @classmethod
    def normalize_reference(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def exact_price(self) -> Self:
        quantum = Decimal(1).scaleb(-CURRENCY_SCALE[self.currency])
        normalized = self.price.quantize(quantum)
        if normalized != self.price:
            raise ValueError("price precision exceeds currency scale")
        object.__setattr__(self, "price", normalized)
        return self

    @classmethod
    def from_offer(
        cls,
        offer: OfferInput,
        *,
        observed_at: datetime,
        source_reliability: float,
        model: str | None = None,
    ) -> "PriceObservation":
        return cls(
            source_name=offer.source_name,
            external_id=offer.external_id,
            features=ProductFeatures.from_offer(offer, model=model),
            price=offer.asking_price,
            currency=offer.currency,
            price_type=PriceType.ASKING,
            observed_at=observed_at,
            source_reliability=source_reliability,
            is_demo=offer.is_demo,
        )


class ProductMatch(FrozenSchema):
    product_match_score: float = Field(ge=0, le=100)
    accepted: bool
    rejection_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class EstimateStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class PriceBasis(StrEnum):
    SOLD = "sold"
    ASKING_BASED = "asking_based"
    NONE = "none"


class Confidence(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ObservationRejection(FrozenSchema):
    source_name: str
    external_id: str
    reasons: tuple[str, ...]
    product_match_score: float | None = Field(default=None, ge=0, le=100)


class EvidenceReference(FrozenSchema):
    source_name: str
    external_id: str
    observed_at: datetime
    evidence_reference: str | None


class MarketEstimate(FrozenSchema):
    product_id: str
    is_demo: bool = Field(strict=True)
    estimated_price: Decimal | None
    currency: str
    price_basis: PriceBasis
    sample_size: int = Field(ge=0)
    median: Decimal | None
    q1: Decimal | None
    q3: Decimal | None
    confidence: Confidence
    estimation_method: str = "median_iqr_type7_v1"
    calculated_at: datetime
    status: EstimateStatus
    confidence_score: float = Field(ge=0, le=1)
    confidence_reasons: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    included_observations: tuple[EvidenceReference, ...] = ()
    rejections: tuple[ObservationRejection, ...] = ()
    outlier_count: int = Field(default=0, ge=0)

    @field_validator("calculated_at")
    @classmethod
    def normalize_date(cls, value: datetime) -> datetime:
        return utc_timestamp(value)

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        statistics = (self.estimated_price, self.median, self.q1, self.q3)
        if self.status == EstimateStatus.INSUFFICIENT_DATA:
            if (
                any(value is not None for value in statistics)
                or self.confidence != Confidence.NONE
                or self.confidence_score != 0
            ):
                raise ValueError("insufficient data cannot produce a market price or confidence")
        elif any(value is None or value <= 0 for value in statistics):
            raise ValueError("successful estimates require positive statistics")
        return self
