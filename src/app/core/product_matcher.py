"""Conservative identity gates followed by a configurable RapidFuzz score."""

from rapidfuzz import fuzz

from app.config import PricingConfig
from app.schemas.pricing import ProductFeatures, ProductMatch
from vinted_deal_hunter.models import Condition

CONDITION_RANK = {
    Condition.NEW_WITH_TAGS: 0,
    Condition.NEW_WITHOUT_TAGS: 1,
    Condition.VERY_GOOD: 2,
    Condition.GOOD: 3,
    Condition.FAIR: 4,
}


class ProductMatcher:
    def __init__(self, config: PricingConfig | None = None) -> None:
        self.config = config if config is not None else PricingConfig()

    def match(self, target: ProductFeatures, candidate: ProductFeatures) -> ProductMatch:
        reasons: list[str] = []
        warnings: list[str] = []
        for field in ("brand", "size"):
            expected, actual = getattr(target, field), getattr(candidate, field)
            if expected is None or actual is None:
                reasons.append(f"{field}_unknown")
            elif expected != actual:
                reasons.append(f"{field}_mismatch")

        model_score = 100.0
        if target.model is None and candidate.model is None:
            model_score = 0.0
            warnings.append("model_unverified")
            if target.product_name != candidate.product_name:
                reasons.append("model_missing_requires_exact_name")
        elif target.model is None or candidate.model is None:
            reasons.append("model_missing")
        elif target.model != candidate.model:
            reasons.append("model_mismatch")

        distance = 0
        target_rank = CONDITION_RANK.get(target.condition) if target.condition is not None else None
        candidate_rank = (
            CONDITION_RANK.get(candidate.condition) if candidate.condition is not None else None
        )
        if target_rank is None or candidate_rank is None:
            reasons.append("condition_unknown")
        else:
            distance = abs(target_rank - candidate_rank)
            if distance > self.config.max_condition_distance:
                reasons.append("condition_mismatch")
            elif distance:
                warnings.append("condition_differs")

        if reasons:
            return ProductMatch(
                product_match_score=0,
                accepted=False,
                rejection_reasons=tuple(reasons),
                warnings=tuple(warnings),
            )
        name_score = (
            100.0
            if target.product_name == candidate.product_name
            else fuzz.token_sort_ratio(target.product_name, candidate.product_name)
        )
        components = {
            "name": name_score,
            "brand": 100.0,
            "model": model_score,
            "size": 100.0,
            "condition": max(0.0, 100 - distance * self.config.condition_step_penalty),
        }
        weights = self.config.match_weights.model_dump()
        total_weight = sum(weights.values())
        score = round(
            sum((weights[key] / total_weight) * value for key, value in components.items()),
            2,
        )
        accepted = score >= self.config.match_threshold
        return ProductMatch(
            product_match_score=score,
            accepted=accepted,
            rejection_reasons=() if accepted else ("below_match_threshold",),
            warnings=tuple(warnings),
        )
