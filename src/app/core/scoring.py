"""Explicit profit calculation and fixed-denominator scoring: missing data adds no points."""

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from app.deal_config import SCORING_VERSION, DealConfig
from app.schemas.deals import DealScore, DealSnapshot, ProfitCalculation, ScoreComponents
from app.schemas.pricing import EstimateStatus, MarketEstimate
from vinted_deal_hunter.models import CURRENCY_SCALE, Condition


def calculate_profit(
    snapshot: DealSnapshot, estimate: MarketEstimate, config: DealConfig
) -> ProfitCalculation:
    offer = snapshot.offer
    if estimate.currency != offer.currency or snapshot.costs.currency != offer.currency:
        raise ValueError("profit calculation requires one currency")
    resale: Decimal | None = None
    discount: Decimal | None = None
    if estimate.status == EstimateStatus.OK:
        assert estimate.estimated_price is not None and estimate.q1 is not None
        quantum = Decimal(1).scaleb(-CURRENCY_SCALE[offer.currency])
        resale = (
            min(estimate.estimated_price, estimate.q1) * (1 - config.resale_haircut)
        ).quantize(
            quantum,
            rounding=ROUND_DOWN,
        )
        discount = (estimate.estimated_price - offer.asking_price) / estimate.estimated_price
    costs = snapshot.costs.total
    profit = None if resale is None or costs is None else resale - offer.asking_price - costs
    return ProfitCalculation(
        currency=offer.currency,
        conservative_resale_price=resale,
        purchase_price=offer.asking_price,
        costs=snapshot.costs,
        total_additional_costs=costs,
        unknown_costs=snapshot.costs.unknown_fields,
        estimated_profit=profit,
        market_discount=discount,
    )


def completeness(snapshot: DealSnapshot) -> Decimal:
    # Fixed denominator: an omitted field never improves completeness.
    fields = (
        bool(snapshot.offer.brand),
        bool(snapshot.model),
        bool(snapshot.offer.size),
        snapshot.offer.condition not in (None, Condition.UNKNOWN),
        bool(snapshot.category),
        bool(snapshot.description),
        snapshot.photo_count is not None and snapshot.photo_count > 0,
        snapshot.offer.published_at is not None,
    )
    return Decimal(sum(fields)) / len(fields)


def known_demand(snapshot: DealSnapshot, as_of: datetime, config: DealConfig) -> Decimal | None:
    evidence = snapshot.demand
    if evidence is None or evidence.available_at > as_of or evidence.observed_at > as_of:
        return None
    if as_of - evidence.observed_at > timedelta(days=config.demand_max_age_days):
        return None
    return evidence.score


def _scaled(value: Decimal | None, full_score: Decimal) -> Decimal:
    if value is None:
        return Decimal(0)
    return max(Decimal(0), min(Decimal(100), value / full_score * 100))


def score_deal(
    snapshot: DealSnapshot,
    estimate: MarketEstimate,
    profit: ProfitCalculation,
    as_of: datetime,
    config: DealConfig,
) -> DealScore:
    demand = known_demand(snapshot, as_of, config)
    missing: list[str] = []
    for name, value in (
        ("profit", profit.estimated_profit),
        ("discount", profit.market_discount),
        ("demand", demand),
    ):
        if value is None:
            missing.append(name)
    if snapshot.offer.condition in (None, Condition.UNKNOWN):
        missing.append("condition")
    if estimate.status != EstimateStatus.OK:
        missing.append("valuation")
    components = ScoreComponents(
        discount=_scaled(profit.market_discount, config.discount_full_score),
        profit=_scaled(profit.estimated_profit, config.profit_full_score),
        confidence=Decimal(str(estimate.confidence_score)) * 100
        if estimate.status == EstimateStatus.OK
        else Decimal(0),
        condition=config.condition_scores.for_condition(snapshot.offer.condition),
        demand=Decimal(0) if demand is None else demand * 100,
        completeness=completeness(snapshot) * 100,
    )
    weights = config.weights.model_dump()
    points = ScoreComponents.model_validate(
        {name: value * weights[name] / 100 for name, value in components.model_dump().items()}
    )
    total = sum(points.model_dump().values(), Decimal(0))
    return DealScore(
        version=SCORING_VERSION,
        total=total,
        components=components,
        weighted_points=points,
        missing_inputs=tuple(missing),
    )
