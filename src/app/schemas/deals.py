"""Immutable deal snapshots, explicit unknown costs, and restartable analysis records."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.ai.schemas import ReviewOutcome
from app.schemas.pricing import (
    FrozenSchema,
    MarketEstimate,
    PriceObservation,
    ProductMatch,
    normalized_label,
    normalized_name,
)
from app.schemas.urls import validate_listing_url
from vinted_deal_hunter.models import CURRENCY_SCALE, OfferInput, clean_text

COST_FIELDS = (
    "buyer_fees",
    "inbound_shipping",
    "resale_fees",
    "outbound_shipping_subsidy",
    "other_costs",
)


def exact_amount(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("money requires an exact decimal string, integer or Decimal")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid money amount") from exc
    if not amount.is_finite() or not Decimal(0) <= amount <= Decimal("1000000000"):
        raise ValueError("money must be finite and nonnegative, at most 1000000000")
    return amount


def required_timestamp(value: object) -> datetime:
    result = OfferInput.parse_date(value)
    if result is None:
        raise ValueError("a timezone-aware timestamp is required")
    return result


class CostBreakdown(FrozenSchema):
    currency: str
    buyer_fees: Decimal | None = None
    inbound_shipping: Decimal | None = None
    resale_fees: Decimal | None = None
    outbound_shipping_subsidy: Decimal | None = None
    other_costs: Decimal | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return OfferInput.normalize_currency(value)

    @field_validator(*COST_FIELDS, mode="before")
    @classmethod
    def parse_amount(cls, value: object) -> Decimal | None:
        return exact_amount(value)

    @model_validator(mode="after")
    def validate_scale(self) -> Self:
        quantum = Decimal(1).scaleb(-CURRENCY_SCALE[self.currency])
        for field in COST_FIELDS:
            amount = getattr(self, field)
            if amount is not None:
                normalized = amount.quantize(quantum)
                if normalized != amount:
                    raise ValueError("cost precision exceeds currency scale")
                object.__setattr__(self, field, normalized)
        return self

    @property
    def unknown_fields(self) -> tuple[str, ...]:
        return tuple(field for field in COST_FIELDS if getattr(self, field) is None)

    @property
    def total(self) -> Decimal | None:
        if self.unknown_fields:
            return None
        return sum((Decimal(getattr(self, field)) for field in COST_FIELDS), Decimal(0))


class DemandEvidence(FrozenSchema):
    score: Decimal = Field(ge=0, le=1)
    source_reference: str = Field(min_length=1, max_length=500)
    observed_at: datetime
    available_at: datetime

    @field_validator("score", mode="before")
    @classmethod
    def parse_score(cls, value: object) -> Decimal:
        result = exact_amount(value)
        if result is None:
            raise ValueError("known demand requires an explicit score")
        return result

    @field_validator("observed_at", "available_at", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime:
        return required_timestamp(value)

    @field_validator("source_reference")
    @classmethod
    def nonblank_reference(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("demand requires an evidence reference")
        return value.strip()

    @model_validator(mode="after")
    def time_order(self) -> Self:
        if self.available_at < self.observed_at:
            raise ValueError("evidence cannot be available before it was observed")
        return self


class DealSnapshot(FrozenSchema):
    offer: OfferInput
    available_at: datetime
    costs: CostBreakdown
    model: Annotated[str | None, Field(max_length=200)] = None
    category: Annotated[str | None, Field(max_length=200)] = None
    description: Annotated[str | None, Field(max_length=10000)] = None
    photo_count: int | None = Field(default=None, ge=0, strict=True)
    demand: DemandEvidence | None = None
    listing_url: str | None = None

    @field_validator("listing_url")
    @classmethod
    def source_url(cls, value: str | None) -> str | None:
        return validate_listing_url(value)

    @field_validator("available_at", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime:
        return required_timestamp(value)

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, value: object) -> object:
        return normalized_label(value) or None if isinstance(value, str) else value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: object) -> object:
        return normalized_name(value) or None if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        return clean_text(value) or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def available_after_publication(self) -> Self:
        if self.offer.published_at is not None and self.available_at < self.offer.published_at:
            raise ValueError("snapshot cannot precede publication")
        return self


class AvailableObservation(FrozenSchema):
    observation: PriceObservation
    available_at: datetime

    @field_validator("available_at", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime:
        return required_timestamp(value)

    @model_validator(mode="after")
    def time_order(self) -> Self:
        if self.available_at < self.observation.observed_at:
            raise ValueError("observation cannot be available before its event time")
        return self


class AnalysisRequest(FrozenSchema):
    snapshot: DealSnapshot
    observations: tuple[AvailableObservation, ...] = ()
    as_of: datetime

    @field_validator("as_of", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime:
        return required_timestamp(value)

    @model_validator(mode="after")
    def no_future_snapshot(self) -> Self:
        if self.snapshot.available_at > self.as_of:
            raise ValueError("cannot analyze a snapshot before it was available")
        return self


class DecisionReason(FrozenSchema):
    code: str
    message: str


class FilterResult(FrozenSchema):
    rejected: tuple[DecisionReason, ...] = ()
    unknown: tuple[DecisionReason, ...] = ()


class ProfitCalculation(FrozenSchema):
    currency: str
    conservative_resale_price: Decimal | None
    purchase_price: Decimal
    costs: CostBreakdown
    total_additional_costs: Decimal | None
    unknown_costs: tuple[str, ...]
    estimated_profit: Decimal | None
    market_discount: Decimal | None


class ScoreComponents(FrozenSchema):
    discount: Decimal = Field(ge=0, le=100)
    profit: Decimal = Field(ge=0, le=100)
    confidence: Decimal = Field(ge=0, le=100)
    condition: Decimal = Field(ge=0, le=100)
    demand: Decimal = Field(ge=0, le=100)
    completeness: Decimal = Field(ge=0, le=100)


class DealScore(FrozenSchema):
    version: str
    total: Decimal = Field(ge=0, le=100)
    components: ScoreComponents
    weighted_points: ScoreComponents
    missing_inputs: tuple[str, ...]


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskFlag(DecisionReason):
    level: RiskLevel
    requires_review: bool


class RiskAssessment(FrozenSchema):
    level: RiskLevel
    flags: tuple[RiskFlag, ...]
    requires_review: bool


class DealState(StrEnum):
    NEW = "NEW"
    NORMALIZED = "NORMALIZED"
    FILTERED = "FILTERED"
    MATCHED = "MATCHED"
    VALUED = "VALUED"
    SCORED = "SCORED"
    AI_PENDING = "AI_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOTIFIED = "NOTIFIED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ERROR = "ERROR"


class StateTransition(FrozenSchema):
    previous: DealState
    current: DealState
    recorded_at: datetime
    reasons: tuple[DecisionReason, ...] = ()


class CandidateMatch(FrozenSchema):
    source_name: str
    external_id: str
    result: ProductMatch


class NotificationReceipt(FrozenSchema):
    receipt_id: str = Field(min_length=1, max_length=500)
    channel: str = Field(min_length=1, max_length=100)
    delivered_at: datetime

    @field_validator("receipt_id", "channel")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("receipt metadata must not be blank")
        return value.strip()

    @field_validator("delivered_at", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime:
        return required_timestamp(value)


class AnalysisRecord(FrozenSchema):
    analysis_id: str
    request: AnalysisRequest
    policy_json: str
    scoring_version: str
    state: DealState = DealState.NEW
    revision: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    resume_state: DealState | None = None
    history: tuple[StateTransition, ...] = ()
    reasons: tuple[DecisionReason, ...] = ()
    pre_filters: FilterResult | None = None
    matches: tuple[CandidateMatch, ...] = ()
    estimate: MarketEstimate | None = None
    profit: ProfitCalculation | None = None
    post_filters: FilterResult | None = None
    score: DealScore | None = None
    risk: RiskAssessment | None = None
    notification_receipt: NotificationReceipt | None = None
    ai_review: ReviewOutcome | None = None


class BatchFailure(FrozenSchema):
    index: int
    error_type: str


class BatchResult(FrozenSchema):
    records: tuple[AnalysisRecord, ...]
    failures: tuple[BatchFailure, ...]
