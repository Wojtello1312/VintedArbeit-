"""Pure eligibility filters. Unknown facts are distinct from failing thresholds."""

import re
from decimal import Decimal

from app.deal_config import DealConfig
from app.schemas.deals import DealSnapshot, DecisionReason, FilterResult, ProfitCalculation
from app.schemas.pricing import MarketEstimate, normalized_name
from vinted_deal_hunter.models import Condition


def pre_filters(snapshot: DealSnapshot, config: DealConfig) -> FilterResult:
    offer = snapshot.offer
    rejected: list[DecisionReason] = []
    unknown: list[DecisionReason] = []
    if offer.currency != config.currency or snapshot.costs.currency != offer.currency:
        rejected.append(
            DecisionReason(
                code="currency_mismatch", message="Offer, policy and costs must share a currency"
            )
        )
    for label, value, allowed in (
        ("brand", offer.brand, config.allowed_brands),
        ("category", snapshot.category, config.allowed_categories),
    ):
        if allowed and value is None:
            unknown.append(
                DecisionReason(code=f"{label}_unknown", message=f"Required {label} is unknown")
            )
        elif allowed and value not in allowed:
            rejected.append(
                DecisionReason(
                    code=f"{label}_excluded", message=f"{label} is outside the allowed set"
                )
            )
    if offer.asking_price < config.min_price:
        rejected.append(
            DecisionReason(
                code="price_below_minimum", message="Purchase price below configured minimum"
            )
        )
    if offer.asking_price > config.max_price:
        rejected.append(
            DecisionReason(
                code="price_above_maximum", message="Purchase price above configured maximum"
            )
        )
    if offer.condition in (None, Condition.UNKNOWN):
        unknown.append(
            DecisionReason(code="condition_unknown", message="Item condition is unknown")
        )
    elif offer.condition not in config.allowed_conditions:
        rejected.append(
            DecisionReason(code="condition_excluded", message="Item condition is not allowed")
        )
    text = normalized_name(f"{offer.product_name} {snapshot.description or ''}")
    if any(
        re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text)
        for word in config.excluded_keywords
    ):
        rejected.append(
            DecisionReason(
                code="excluded_keyword",
                message="Listing contains a configured excluded word or phrase",
            )
        )
    return FilterResult(rejected=tuple(rejected), unknown=tuple(unknown))


def post_filters(
    estimate: MarketEstimate,
    profit: ProfitCalculation,
    minimum_match_score: Decimal | None,
    config: DealConfig,
) -> FilterResult:
    rejected: list[DecisionReason] = []
    unknown: list[DecisionReason] = []
    for label, value, minimum in (
        ("match_score", minimum_match_score, config.min_match_score),
        ("comparisons", Decimal(estimate.sample_size), Decimal(config.min_comparisons)),
        ("profit", profit.estimated_profit, config.min_profit),
        ("market_discount", profit.market_discount, config.min_market_discount),
    ):
        if value is None:
            unknown.append(
                DecisionReason(code=f"{label}_unknown", message=f"Cannot verify minimum {label}")
            )
        elif value < minimum:
            rejected.append(
                DecisionReason(
                    code=f"{label}_below_minimum", message=f"{label} below configured minimum"
                )
            )
    return FilterResult(rejected=tuple(rejected), unknown=tuple(unknown))
