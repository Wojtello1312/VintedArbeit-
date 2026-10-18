from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.filters import post_filters, pre_filters
from app.core.risk_engine import assess_risk
from app.core.scoring import calculate_profit, completeness, known_demand, score_deal
from app.deal_config import DealConfig, ScoreWeights
from app.schemas.deals import (
    COST_FIELDS,
    CostBreakdown,
    DealSnapshot,
    DemandEvidence,
    ProfitCalculation,
)
from app.schemas.pricing import MarketEstimate


def changed(snapshot: DealSnapshot, **updates: object) -> DealSnapshot:
    return DealSnapshot.model_validate({**snapshot.model_dump(), **updates})


@pytest.mark.parametrize(
    ("price", "reason"),
    [
        ("9.99", "price_below_minimum"),
        ("10", None),
        ("10.01", None),
        ("99.99", None),
        ("100", None),
        ("100.01", "price_above_maximum"),
    ],
)
def test_price_filter_boundaries(snapshot: DealSnapshot, price: str, reason: str | None) -> None:
    target = changed(snapshot, offer={**snapshot.offer.model_dump(), "asking_price": price})
    result = pre_filters(target, DealConfig(min_price=Decimal(10), max_price=Decimal(100)))
    assert [item.code for item in result.rejected] == ([] if reason is None else [reason])


def test_brand_category_condition_and_words(snapshot: DealSnapshot) -> None:
    policy = DealConfig(
        allowed_brands=("DEMO BRAND",),
        allowed_categories=("SNEAKERS",),
        excluded_keywords=("replica",),
    )
    assert not pre_filters(snapshot, policy).rejected
    assert (
        pre_filters(changed(snapshot, category="bags"), policy).rejected[0].code
        == "category_excluded"
    )
    assert (
        pre_filters(changed(snapshot, category=None), policy).unknown[0].code == "category_unknown"
    )
    assert (
        pre_filters(
            changed(snapshot, offer={**snapshot.offer.model_dump(), "brand": "Other"}), policy
        )
        .rejected[0]
        .code
        == "brand_excluded"
    )
    assert (
        pre_filters(
            changed(snapshot, offer={**snapshot.offer.model_dump(), "condition": "unknown"}), policy
        )
        .unknown[0]
        .code
        == "condition_unknown"
    )
    assert (
        pre_filters(changed(snapshot, description="DEMO REPLICA!"), policy).rejected[0].code
        == "excluded_keyword"
    )
    assert not pre_filters(changed(snapshot, description="DEMO replication test"), policy).rejected


def test_every_cost_is_subtracted_explicitly(
    snapshot: DealSnapshot, estimate: MarketEstimate
) -> None:
    costs = CostBreakdown.model_validate(
        {"currency": "PLN", **dict(zip(COST_FIELDS, ["2", "3", "4", "5", "6"], strict=True))}
    )
    result = calculate_profit(changed(snapshot, costs=costs), estimate, DealConfig())
    assert result.conservative_resale_price == Decimal("180.00")
    assert result.purchase_price == Decimal("40.00")
    assert result.total_additional_costs == Decimal(20)
    assert result.estimated_profit == Decimal("120.00")
    assert result.market_discount == Decimal("0.8")
    assert result.costs == costs


@pytest.mark.parametrize("field", COST_FIELDS)
def test_each_unknown_cost_blocks_profit(
    snapshot: DealSnapshot,
    estimate: MarketEstimate,
    deal_now: datetime,
    field: str,
) -> None:
    target = changed(snapshot, costs={**snapshot.costs.model_dump(), field: None})
    profit = calculate_profit(target, estimate, DealConfig())
    assert profit.estimated_profit is None and profit.total_additional_costs is None
    assert profit.unknown_costs == (field,)
    score = score_deal(target, estimate, profit, deal_now, DealConfig())
    assert score.components.profit == 0
    assert assess_risk(target, estimate, profit, deal_now, DealConfig()).requires_review


def test_explicit_zero_and_missing_are_different(
    snapshot: DealSnapshot, estimate: MarketEstimate
) -> None:
    zero = CostBreakdown.model_validate({"currency": "PLN", **dict.fromkeys(COST_FIELDS, "0")})
    unknown = CostBreakdown(currency="PLN")
    assert calculate_profit(
        changed(snapshot, costs=zero), estimate, DealConfig()
    ).estimated_profit == Decimal(140)
    assert (
        calculate_profit(changed(snapshot, costs=unknown), estimate, DealConfig()).estimated_profit
        is None
    )


@pytest.mark.parametrize("value", [-1, True, 0.1, "NaN", "Infinity", "1.001"])
def test_invalid_costs_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        CostBreakdown.model_validate({"currency": "PLN", "buyer_fees": value})


def test_conservative_price_rounds_down(snapshot: DealSnapshot, estimate: MarketEstimate) -> None:
    tiny = MarketEstimate.model_validate(
        {
            **estimate.model_dump(),
            "estimated_price": "1.01",
            "q1": "1.01",
            "median": "1.01",
            "q3": "1.01",
        }
    )
    result = calculate_profit(snapshot, tiny, DealConfig())
    assert result.conservative_resale_price == Decimal("0.90")
    assert result.estimated_profit < 0


def test_currency_mismatch_does_not_compute_profit(
    snapshot: DealSnapshot, estimate: MarketEstimate
) -> None:
    target = changed(snapshot, costs={**snapshot.costs.model_dump(), "currency": "EUR"})
    assert pre_filters(target, DealConfig()).rejected[0].code == "currency_mismatch"
    with pytest.raises(ValueError):
        calculate_profit(target, estimate, DealConfig())


@pytest.mark.parametrize(
    ("field", "threshold", "step"),
    [
        ("match_score", Decimal(80), Decimal("0.01")),
        ("comparisons", Decimal(5), Decimal(1)),
        ("profit", Decimal(20), Decimal("0.01")),
        ("market_discount", Decimal("0.2"), Decimal("0.0001")),
    ],
)
def test_post_filter_thresholds_are_inclusive(
    snapshot: DealSnapshot,
    estimate: MarketEstimate,
    field: str,
    threshold: Decimal,
    step: Decimal,
) -> None:
    for value in (threshold - step, threshold, threshold + step):
        profit = calculate_profit(snapshot, estimate, DealConfig())
        valuation = estimate
        match_score = Decimal(100)
        if field == "match_score":
            match_score = value
        elif field == "comparisons":
            valuation = MarketEstimate.model_validate(
                {**estimate.model_dump(), "sample_size": int(value)}
            )
        else:
            key = "estimated_profit" if field == "profit" else "market_discount"
            profit = ProfitCalculation.model_validate({**profit.model_dump(), key: value})
        result = post_filters(valuation, profit, match_score, DealConfig())
        codes = {reason.code for reason in result.rejected}
        assert (f"{field}_below_minimum" in codes) == (value < threshold)


def test_default_weights_and_missing_data_do_not_raise_score(
    snapshot: DealSnapshot,
    estimate: MarketEstimate,
    deal_now: datetime,
) -> None:
    config = DealConfig()
    assert tuple(config.weights.model_dump().values()) == (30, 25, 20, 10, 10, 5)
    profit = calculate_profit(snapshot, estimate, config)
    original = score_deal(snapshot, estimate, profit, deal_now, config)
    assert original.total == sum(original.weighted_points.model_dump().values())
    for field in ("model", "category", "description", "photo_count", "demand"):
        target = changed(snapshot, **{field: None})
        score = score_deal(target, estimate, profit, deal_now, config)
        assert score.total <= original.total
    unknown = score_deal(changed(snapshot, demand=None), estimate, profit, deal_now, config)
    assert unknown.components.demand == 0 and "demand" in unknown.missing_inputs
    assert original.total - unknown.total == Decimal(8)
    assert completeness(changed(snapshot, description=None)) < completeness(snapshot)


@pytest.mark.parametrize(
    ("days", "available_offset", "known"),
    [(30, 0, True), (31, 0, False), (0, 1, False), (0, 0, True)],
)
def test_demand_availability_boundaries(
    snapshot: DealSnapshot, deal_now: datetime, days: int, available_offset: int, known: bool
) -> None:
    demand = DemandEvidence(
        score=Decimal(0),
        source_reference="DEMO known zero demand",
        observed_at=deal_now - timedelta(days=days),
        available_at=deal_now + timedelta(seconds=available_offset),
    )
    assert (
        known_demand(changed(snapshot, demand=demand), deal_now, DealConfig()) is not None
    ) == known


@pytest.mark.parametrize(
    "weights",
    [{"discount": 29}, {"profit": 26}, {"discount": -1}, {"discount": True}, {"discount": 30.0}],
)
def test_invalid_weights(weights: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScoreWeights.model_validate(weights)


def test_score_components_respect_custom_weights(
    snapshot: DealSnapshot, estimate: MarketEstimate, deal_now: datetime
) -> None:
    config = DealConfig(
        weights=ScoreWeights(
            discount=0, profit=0, confidence=0, condition=0, demand=100, completeness=0
        )
    )
    score = score_deal(
        snapshot, estimate, calculate_profit(snapshot, estimate, config), deal_now, config
    )
    assert score.total == Decimal(80)
    assert (
        score_deal(
            changed(snapshot, demand=None),
            estimate,
            calculate_profit(snapshot, estimate, config),
            deal_now,
            config,
        ).total
        == 0
    )


@pytest.mark.parametrize(
    ("value", "expected"), [("99.99", "99.99"), ("100", "100"), ("100.01", "100"), ("-0.01", "0")]
)
def test_profit_score_saturates_without_crossing_bounds(
    snapshot: DealSnapshot,
    estimate: MarketEstimate,
    deal_now: datetime,
    value: str,
    expected: str,
) -> None:
    profit = calculate_profit(snapshot, estimate, DealConfig())
    profit = ProfitCalculation.model_validate({**profit.model_dump(), "estimated_profit": value})
    result = score_deal(snapshot, estimate, profit, deal_now, DealConfig())
    assert result.components.profit == Decimal(expected)


def test_risk_thresholds_are_inclusive(
    snapshot: DealSnapshot, estimate: MarketEstimate, deal_now: datetime
) -> None:
    profit = calculate_profit(snapshot, estimate, DealConfig())
    policy = DealConfig(min_valuation_confidence=Decimal("0.6"), min_completeness=Decimal(1))
    for confidence, low in [(0.5999, True), (0.6, False), (0.6001, False)]:
        valuation = MarketEstimate.model_validate(
            {**estimate.model_dump(), "confidence_score": confidence}
        )
        risk = assess_risk(snapshot, valuation, profit, deal_now, policy)
        assert ("low_valuation_confidence" in {flag.code for flag in risk.flags}) == low
        assert "incomplete_listing" not in {flag.code for flag in risk.flags}
    less_complete = assess_risk(
        changed(snapshot, description=None), estimate, profit, deal_now, policy
    )
    assert "incomplete_listing" in {flag.code for flag in less_complete.flags}


@pytest.mark.parametrize("value", [True, "unknown", "NaN", "-0.01", "1.01", None])
def test_invalid_demand_never_becomes_high_demand(deal_now: datetime, value: object) -> None:
    with pytest.raises(ValidationError):
        DemandEvidence.model_validate(
            {
                "score": value,
                "source_reference": "DEMO",
                "observed_at": deal_now,
                "available_at": deal_now,
            }
        )
