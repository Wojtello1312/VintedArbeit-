"""Evidence-based risk flags, independent of persistence and scoring thresholds."""

from datetime import datetime
from decimal import Decimal

from app.core.scoring import completeness, known_demand
from app.deal_config import DealConfig
from app.schemas.deals import DealSnapshot, ProfitCalculation, RiskAssessment, RiskFlag, RiskLevel
from app.schemas.pricing import MarketEstimate, PriceBasis


def assess_risk(
    snapshot: DealSnapshot,
    estimate: MarketEstimate,
    profit: ProfitCalculation,
    as_of: datetime,
    config: DealConfig,
) -> RiskAssessment:
    flags: list[RiskFlag] = []

    def add(code: str, message: str, level: RiskLevel, review: bool) -> None:
        flags.append(RiskFlag(code=code, message=message, level=level, requires_review=review))

    if profit.unknown_costs:
        add(
            "unknown_costs",
            "Unknown costs prevent a complete profit calculation",
            RiskLevel.HIGH,
            True,
        )
    if estimate.price_basis == PriceBasis.ASKING_BASED:
        add(
            "asking_based",
            "Valuation uses asking prices rather than completed transactions",
            RiskLevel.MEDIUM,
            config.review_asking_based,
        )
    if Decimal(str(estimate.confidence_score)) < config.min_valuation_confidence:
        add(
            "low_valuation_confidence",
            "Valuation confidence below policy minimum",
            RiskLevel.MEDIUM,
            True,
        )
    if known_demand(snapshot, as_of, config) is None:
        add(
            "unknown_demand",
            "Demand evidence is absent, stale or not yet available",
            RiskLevel.MEDIUM,
            config.require_known_demand,
        )
    if completeness(snapshot) < config.min_completeness:
        add(
            "incomplete_listing",
            "Listing completeness below policy minimum",
            RiskLevel.MEDIUM,
            True,
        )
    if profit.estimated_profit is not None and profit.estimated_profit < 0:
        add("predicted_loss", "Known costs imply a negative profit", RiskLevel.HIGH, False)
    level = (
        RiskLevel.HIGH
        if any(flag.level == RiskLevel.HIGH for flag in flags)
        else (RiskLevel.MEDIUM if flags else RiskLevel.LOW)
    )
    return RiskAssessment(
        level=level, flags=tuple(flags), requires_review=any(flag.requires_review for flag in flags)
    )
