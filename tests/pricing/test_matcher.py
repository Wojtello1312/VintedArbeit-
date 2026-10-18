import pytest

from app.config import MatchWeights, PricingConfig
from app.core.product_matcher import ProductMatcher
from app.schemas.pricing import ProductFeatures
from vinted_deal_hunter.models import Condition


def changed(features: ProductFeatures, **values: object) -> ProductFeatures:
    return ProductFeatures.model_validate({**features.model_dump(), **values})


def test_normalization_and_exact_identity(features: ProductFeatures) -> None:
    candidate = changed(
        features,
        product_name=" NIKE   Air-Max 90 ",
        brand=" NIKE ",
        model=" AIR  MAX 90 ",
        size=" eu 42 ",
        condition="bardzo dobry",
    )
    assert candidate == features
    result = ProductMatcher().match(features, candidate)
    assert result.accepted and result.product_match_score == 100
    assert result.rejection_reasons == ()


def test_rapidfuzz_and_configurable_cutoff(features: ProductFeatures) -> None:
    candidate = changed(features, product_name="Nike Air Max 90 blue")
    result = ProductMatcher().match(features, candidate)
    assert result.accepted and 80 < result.product_match_score < 100
    stricter = ProductMatcher(PricingConfig(match_threshold=100)).match(features, candidate)
    assert not stricter.accepted
    assert stricter.rejection_reasons == ("below_match_threshold",)
    reordered = changed(features, product_name="Air Max 90 Nike")
    assert ProductMatcher().match(features, reordered).product_match_score == 100


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("model", "Air Max 95", "model_mismatch"),
        ("size", "EU 43", "size_mismatch"),
        ("size", "42", "size_mismatch"),
        ("brand", "Adidas", "brand_mismatch"),
        ("condition", "fair", "condition_mismatch"),
        ("condition", "unknown", "condition_unknown"),
        ("size", None, "size_unknown"),
        ("brand", None, "brand_unknown"),
    ],
)
def test_identity_gates_cannot_be_bypassed_by_lower_threshold(
    features: ProductFeatures,
    field: str,
    value: object,
    reason: str,
) -> None:
    result = ProductMatcher(PricingConfig(match_threshold=0)).match(
        features, changed(features, **{field: value})
    )
    assert not result.accepted
    assert result.product_match_score == 0
    assert reason in result.rejection_reasons


def test_missing_models_require_exact_name(features: ProductFeatures) -> None:
    target = changed(features, model=None)
    result = ProductMatcher().match(target, target)
    assert result.accepted and result.product_match_score == 80
    assert result.warnings == ("model_unverified",)
    different_model_in_name = changed(target, product_name="Nike Air Max 95")
    assert ProductMatcher().match(target, different_model_in_name).rejection_reasons == (
        "model_missing_requires_exact_name",
    )
    assert ProductMatcher().match(features, target).rejection_reasons == ("model_missing",)


def test_condition_relaxation_is_explicit(features: ProductFeatures) -> None:
    worse = changed(features, condition=Condition.GOOD)
    matcher = ProductMatcher(PricingConfig(max_condition_distance=1))
    result = matcher.match(features, worse)
    assert result.accepted and result.product_match_score < 100
    assert "condition_differs" in result.warnings


def test_custom_weights_are_used(features: ProductFeatures) -> None:
    config = PricingConfig(
        match_weights=MatchWeights(name=1, brand=0, model=0, size=0, condition=0)
    )
    candidate = changed(features, product_name="Nike Air Max 90 blue")
    assert ProductMatcher(config).match(features, candidate).product_match_score < (
        ProductMatcher().match(features, candidate).product_match_score
    )
