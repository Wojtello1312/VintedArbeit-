"""Validated decision policy; monetary thresholds refer to one explicit currency."""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.schemas.pricing import FrozenSchema, normalized_label, normalized_name
from vinted_deal_hunter.models import Condition, OfferInput

Percentage = Annotated[Decimal, Field(ge=0, le=100)]
Ratio = Annotated[Decimal, Field(ge=0, le=1)]
Amount = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000"))]
SCORING_VERSION = "deal-score-v1"
PIPELINE_VERSION = "deal-pipeline-v1"


class ScoreWeights(FrozenSchema):
    discount: int = Field(default=30, ge=0, le=100, strict=True)
    profit: int = Field(default=25, ge=0, le=100, strict=True)
    confidence: int = Field(default=20, ge=0, le=100, strict=True)
    condition: int = Field(default=10, ge=0, le=100, strict=True)
    demand: int = Field(default=10, ge=0, le=100, strict=True)
    completeness: int = Field(default=5, ge=0, le=100, strict=True)

    @model_validator(mode="after")
    def sum_to_hundred(self) -> Self:
        if sum(self.model_dump().values()) != 100:
            raise ValueError("score weights must sum to exactly 100")
        return self


class ConditionScores(FrozenSchema):
    new_with_tags: Percentage = Decimal(100)
    new_without_tags: Percentage = Decimal(90)
    very_good: Percentage = Decimal(80)
    good: Percentage = Decimal(60)
    fair: Percentage = Decimal(30)

    def for_condition(self, condition: Condition | None) -> Decimal:
        if condition is None or condition == Condition.UNKNOWN:
            return Decimal(0)
        return Decimal(self.model_dump()[condition.value])


class DealConfig(FrozenSchema):
    currency: str = "PLN"
    allowed_brands: tuple[str, ...] = ()
    allowed_categories: tuple[str, ...] = ()
    allowed_conditions: tuple[Condition, ...] = (
        Condition.NEW_WITH_TAGS,
        Condition.NEW_WITHOUT_TAGS,
        Condition.VERY_GOOD,
        Condition.GOOD,
    )
    excluded_keywords: tuple[str, ...] = ()
    min_price: Amount = Decimal(0)
    max_price: Amount = Decimal(1000)
    min_match_score: Percentage = Decimal(80)
    min_comparisons: int = Field(default=5, ge=2)
    min_profit: Amount = Decimal(20)
    min_market_discount: Ratio = Decimal("0.20")
    resale_haircut: Ratio = Decimal("0.10")
    discount_full_score: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    profit_full_score: Decimal = Field(default=Decimal(100), gt=0)
    approval_score: Percentage = Decimal(70)
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    condition_scores: ConditionScores = Field(default_factory=ConditionScores)
    min_valuation_confidence: Ratio = Decimal("0.60")
    min_completeness: Ratio = Decimal("0.50")
    demand_max_age_days: int = Field(default=30, ge=1, le=36500)
    review_asking_based: bool = True
    require_known_demand: bool = False
    ai_review_required: bool = False
    max_attempts: int = Field(default=3, ge=1, le=100)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return OfferInput.normalize_currency(value)

    @field_validator("allowed_brands")
    @classmethod
    def normalize_brands(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(OfferInput.normalize_brand(normalized_label(value)) for value in values)
        if any(not value for value in normalized):
            raise ValueError("brand filters cannot contain empty labels")
        return tuple(value for value in normalized if value is not None)

    @field_validator("allowed_categories", "excluded_keywords")
    @classmethod
    def normalize_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalized_name(value) for value in values)
        if any(not value for value in normalized):
            raise ValueError("filters cannot contain empty labels")
        return normalized

    @model_validator(mode="after")
    def coherent_range(self) -> Self:
        if self.min_price > self.max_price:
            raise ValueError("min_price cannot exceed max_price")
        if Condition.UNKNOWN in self.allowed_conditions:
            raise ValueError("unknown condition cannot be approved by configuration")
        return self
