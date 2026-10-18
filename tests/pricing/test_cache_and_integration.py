from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import ConfidenceWeights, MatchWeights, PricingConfig
from app.core.price_estimator import PriceEstimator
from app.schemas.pricing import (
    Confidence,
    EstimateStatus,
    PriceBasis,
    PriceObservation,
    PricingProduct,
    ProductFeatures,
)
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.models import OfferInput
from vinted_deal_hunter.sources import JSONOfferSource


def test_cache_reuses_normalized_equivalent_product_and_preserves_id(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    now = pricing_now
    estimator = PriceEstimator(clock=lambda: now)
    observations = [observation_factory(i) for i in range(5)]
    first = estimator.estimate(product, observations)
    equivalent = PricingProduct.model_validate(
        {
            **product.model_dump(),
            "product_id": "ANOTHER-DEMO-PRODUCT",
            "features": {**product.features.model_dump(), "product_name": " NIKE  AIR-MAX 90 "},
        }
    )
    now += timedelta(seconds=1)
    second = estimator.estimate(equivalent, reversed(observations))
    assert estimator.cache_info().hits == 1
    assert second.product_id == "ANOTHER-DEMO-PRODUCT"
    assert first.product_id == "DEMO-PRODUCT"
    assert second.calculated_at == first.calculated_at
    with pytest.raises(ValidationError):
        second.estimated_price = Decimal("999")


def test_changed_prices_and_reliability_invalidate_cache(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    estimator = PriceEstimator(clock=lambda: pricing_now)
    observations = [observation_factory(i) for i in range(5)]
    estimator.estimate(product, observations)
    changed = [observation_factory(i, "200") for i in range(5)]
    assert estimator.estimate(product, changed).estimated_price == Decimal("200")
    unreliable = [observation_factory(i, source_reliability=0.1) for i in range(5)]
    assert estimator.estimate(product, unreliable).status == EstimateStatus.INSUFFICIENT_DATA
    assert estimator.cache_info().hits == 0


def test_ttl_and_cache_size_limit(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    now = pricing_now
    estimator = PriceEstimator(
        PricingConfig(cache_ttl_seconds=10, cache_max_entries=1), clock=lambda: now
    )
    observations = [observation_factory(i) for i in range(5)]
    first = estimator.estimate(product, observations)
    now += timedelta(seconds=10)
    assert estimator.estimate(product, observations).calculated_at > first.calculated_at
    estimator.estimate(product, [observation_factory(i, "200") for i in range(5)])
    assert estimator.cache_info().size == 1
    estimator.clear_cache()
    assert estimator.cache_info().size == 0


@pytest.mark.parametrize("boundary", ["stale", "future"])
def test_cache_does_not_keep_observations_across_age_boundaries(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
    boundary: str,
) -> None:
    now = pricing_now
    estimator = PriceEstimator(clock=lambda: now)
    observed = now - timedelta(days=90) if boundary == "stale" else now + timedelta(seconds=1)
    observations = [observation_factory(i, observed_at=observed) for i in range(5)]
    first = estimator.estimate(product, observations)
    now += timedelta(seconds=2)
    second = estimator.estimate(product, observations)
    assert first.status != second.status
    assert estimator.cache_info().hits == 0


def test_self_exclusion_is_part_of_cache_identity(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    estimator = PriceEstimator(clock=lambda: pricing_now)
    observations = [observation_factory(i) for i in range(5)]
    assert estimator.estimate(product, observations).sample_size == 5
    target = PricingProduct.model_validate(
        {**product.model_dump(), "source_name": "demo-market", "external_id": "DEMO-0"}
    )
    assert estimator.estimate(target, observations).sample_size == 4


def test_observation_source_failures_are_not_empty_market(
    product: PricingProduct,
    pricing_now: datetime,
) -> None:
    class FailingSource:
        def fetch_comparables(self, product: PricingProduct) -> Sequence[PriceObservation]:
            raise RuntimeError("source unavailable")

    with pytest.raises(RuntimeError, match="source unavailable"):
        PriceEstimator(clock=lambda: pricing_now).estimate_from_source(product, FailingSource())


def test_imported_asking_offers_can_be_priced_offline(
    settings: Settings,
    repository: SQLiteOfferRepository,
    pricing_now: datetime,
    raw_offer: dict[str, object],
) -> None:
    import json

    rows = [
        {**raw_offer, "external_id": f"DEMO-COMPARABLE-{i}", "asking_price": str(100 + i)}
        for i in range(6)
    ]
    source = JSONOfferSource(json.dumps(rows).encode(), source_name="demo", is_demo=True)
    assert import_offers(source, repository, settings).new == 6
    offers = repository.list_offers()
    # The caller supplies observation time and source reliability; ingestion invents neither.
    observations = [
        PriceObservation.from_offer(offer, observed_at=pricing_now, source_reliability=0.9)
        for offer in offers
    ]
    target = PricingProduct.from_offer(offers[0])
    result = PriceEstimator(clock=lambda: pricing_now).estimate(target, observations)
    assert result.status == EstimateStatus.OK
    assert result.sample_size == 5  # Target listing is not its own comparable.
    assert result.estimated_price == Decimal("103")
    assert result.price_basis == PriceBasis.ASKING_BASED and result.confidence == Confidence.LOW
    assert "model_unverified" in result.confidence_reasons
    assert all(not item.sale_confirmed for item in observations)
    assert repository.list_offers() == offers


@pytest.mark.parametrize(
    "options",
    [
        {"match_threshold": -1},
        {"match_threshold": 101},
        {"min_observations": 1},
        {"iqr_multiplier": -1},
        {"iqr_multiplier": float("nan")},
        {"confidence_high_threshold": 0.5},
        {"cache_ttl_seconds": -1},
        {"confidence_full_sample_size": 4},
        {"min_source_reliability": 1.1},
    ],
)
def test_invalid_policies_are_rejected(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PricingConfig.model_validate(options)


def test_weights_and_environment_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        MatchWeights(name=0, brand=0, model=0, size=0, condition=0)
    with pytest.raises(ValidationError):
        ConfidenceWeights(sample=-1)
    monkeypatch.setenv("VDH_PRICING", '{"match_threshold":92,"min_observations":7}')
    assert Settings.from_env().pricing.match_threshold == 92
    assert Settings.from_env().pricing.min_observations == 7


def test_observation_schema_preserves_decimal_and_requires_timezone(
    observation_factory: Callable[..., PriceObservation],
    pricing_now: datetime,
) -> None:
    observation = observation_factory(1, "0.29")
    assert observation.price == Decimal("0.29")
    assert PriceObservation.model_validate_json(observation.model_dump_json()) == observation
    with pytest.raises(ValidationError):
        observation_factory(1, observed_at=pricing_now.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        observation_factory(1, "1.123")


def test_product_features_from_offer_does_not_infer_model(raw_offer: dict[str, object]) -> None:
    offer = OfferInput.model_validate({**raw_offer, "source_name": "demo", "is_demo": True})
    assert ProductFeatures.from_offer(offer).model is None
    assert ProductFeatures.from_offer(offer, model=" VERIFIED MODEL ").model == "verified model"
