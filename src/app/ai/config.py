"""AI policy contains no credentials. Unknown model pricing disables paid calls."""

import os
from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator

from app.schemas.pricing import FrozenSchema


class AIConfig(FrozenSchema):
    enabled: bool = False
    allow_demo: bool = False
    model: str = Field(default="gpt-5-nano", min_length=1, max_length=200)
    db_path: Path = Path(".data/ai.sqlite3")
    min_score: Decimal = Field(default=Decimal(65), ge=65, le=100)
    min_valuation_confidence: float = Field(default=0.60, ge=0, le=1)
    min_comparisons: int = Field(default=5, ge=1, strict=True)
    min_review_confidence: float = Field(default=0.60, ge=0, le=1)
    min_listing_consistency: int = Field(default=70, ge=0, le=100, strict=True)
    daily_budget_usd: Decimal = Field(default=Decimal("1"), ge=0, le=1000000)
    max_daily_analyses: int = Field(default=100, ge=0, le=1000000, strict=True)
    # Explicit deployment-specific rates, USD / million tokens. No guessed tariffs.
    input_usd_per_million: Decimal | None = Field(default=None, gt=0, le=1000000)
    output_usd_per_million: Decimal | None = Field(default=None, gt=0, le=1000000)
    cached_input_usd_per_million: Decimal | None = Field(default=None, ge=0, le=1000000)
    timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_attempts: int = Field(default=3, ge=1, le=5, strict=True)
    backoff_seconds: float = Field(default=1, gt=0, le=30)
    max_backoff_seconds: float = Field(default=10, gt=0, le=60)
    max_input_tokens: int = Field(default=32000, ge=1000, le=200000, strict=True)
    max_output_tokens: int = Field(default=2000, ge=128, le=16000, strict=True)

    @field_validator(
        "daily_budget_usd",
        "input_usd_per_million",
        "output_usd_per_million",
        "cached_input_usd_per_million",
        mode="before",
    )
    @classmethod
    def exact_dollars(cls, value: object) -> object:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (str, int, Decimal))
        ):
            raise ValueError("USD values require Decimal or exact strings/integers")
        return value

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def coherent_limits(self) -> Self:
        if self.max_backoff_seconds < self.backoff_seconds:
            raise ValueError("max backoff must cover initial backoff")
        if (
            self.cached_input_usd_per_million is not None
            and self.input_usd_per_million is not None
            and self.cached_input_usd_per_million > self.input_usd_per_million
        ):
            raise ValueError("cached input rate cannot exceed regular input rate")
        return self

    @property
    def pricing_configured(self) -> bool:
        return self.input_usd_per_million is not None and self.output_usd_per_million is not None

    @classmethod
    def from_env(cls) -> "AIConfig":
        config = cls.model_validate_json(os.environ.get("VDH_AI", "{}"))
        if "OPENAI_MODEL" in os.environ:
            config = config.model_copy(
                update={"model": cls(model=os.environ["OPENAI_MODEL"]).model}
            )
        return config
