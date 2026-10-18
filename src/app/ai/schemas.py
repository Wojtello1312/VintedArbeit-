"""Validated review contract. There is deliberately no AI-generated price field."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.schemas.pricing import FrozenSchema, utc_timestamp


class AIReview(FrozenSchema):
    listing_consistency: int = Field(ge=0, le=100, strict=True)
    risk_level: Literal["low", "medium", "high", "unknown"]
    detected_issues: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        max_length=30
    )
    product_match_valid: bool | None = Field(strict=True)
    manual_review_required: bool = Field(strict=True)
    explanation: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1, strict=True)

    @field_validator("explanation")
    @classmethod
    def nonblank_explanation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("explanation must not be blank")
        return value

    @model_validator(mode="after")
    def enforce_manual_review(self) -> Self:
        if self.risk_level in {"high", "unknown"} or self.product_match_valid is not True:
            object.__setattr__(self, "manual_review_required", True)
        return self


class TokenUsage(FrozenSchema):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    cached_input_tokens: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def valid_cached_count(self) -> Self:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached tokens cannot exceed input tokens")
        return self


class StoredReview(FrozenSchema):
    input_hash: str
    model: str
    prompt_version: str
    reviewed_at: datetime
    result: AIReview
    usage: TokenUsage | None
    # Unknown charges remain unknown; the reservation is tracked separately.
    cost_usd: Decimal | None
    reserved_usd: Decimal

    @field_validator("reviewed_at")
    @classmethod
    def utc_date(cls, value: datetime) -> datetime:
        return utc_timestamp(value)


class ReviewOutcome(FrozenSchema):
    status: Literal["completed", "cached", "skipped", "unavailable", "pending"]
    reason: str
    review: StoredReview | None = None
    manual_review_required: bool = False

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        if (self.status in {"completed", "cached"}) != (self.review is not None):
            raise ValueError("completed/cached outcomes require a validated review")
        if self.review is not None and self.review.result.manual_review_required:
            object.__setattr__(self, "manual_review_required", True)
        return self
