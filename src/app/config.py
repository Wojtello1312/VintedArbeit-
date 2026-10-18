"""Immutable, validated pricing policy. These values are heuristics, not learned facts."""

import math
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Weights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def positive_total(self) -> Self:
        total = sum(self.model_dump().values())
        if not math.isfinite(total) or total <= 0:
            raise ValueError("weights must have a positive finite total")
        return self


class MatchWeights(Weights):
    name: float = Field(default=0.45, ge=0)
    brand: float = Field(default=0.15, ge=0)
    model: float = Field(default=0.20, ge=0)
    size: float = Field(default=0.10, ge=0)
    condition: float = Field(default=0.10, ge=0)


class ConfidenceWeights(Weights):
    sample: float = Field(default=0.35, ge=0)
    match: float = Field(default=0.25, ge=0)
    reliability: float = Field(default=0.25, ge=0)
    dispersion: float = Field(default=0.15, ge=0)


class PricingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    match_threshold: float = Field(default=80, ge=0, le=100)
    match_weights: MatchWeights = Field(default_factory=MatchWeights)
    max_condition_distance: int = Field(default=0, ge=0, le=4)
    condition_step_penalty: float = Field(default=25, ge=0, le=100)
    min_observations: int = Field(default=5, ge=2)
    min_source_reliability: float = Field(default=0.8, ge=0, le=1)
    max_observation_age_days: int = Field(default=90, ge=1, le=36500)
    iqr_multiplier: float = Field(default=1.5, ge=0)
    confidence_weights: ConfidenceWeights = Field(default_factory=ConfidenceWeights)
    confidence_full_sample_size: int = Field(default=20, ge=2)
    confidence_medium_threshold: float = Field(default=0.60, gt=0, lt=1)
    confidence_high_threshold: float = Field(default=0.85, gt=0, le=1)
    asking_confidence_cap: float = Field(default=0.59, ge=0, le=1)
    missing_model_confidence_cap: float = Field(default=0.59, ge=0, le=1)
    cache_ttl_seconds: int = Field(default=300, ge=0, le=86400)
    cache_max_entries: int = Field(default=256, ge=1)

    @model_validator(mode="after")
    def consistent_thresholds(self) -> Self:
        if self.confidence_medium_threshold >= self.confidence_high_threshold:
            raise ValueError("medium confidence threshold must be lower than high")
        if self.confidence_full_sample_size < self.min_observations:
            raise ValueError("full confidence sample cannot be smaller than minimum sample")
        return self
