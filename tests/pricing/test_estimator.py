from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.config import PricingConfig
from app.core.price_estimator import PriceEstimator, quantile
from app.schemas.pricing import (
    Confidence,
    EstimateStatus,
    PriceBasis,
    PriceObservation,
    PriceType,
    PricingProduct,
)


@pytest.mark.parametrize("size", [0, 1, 4])
def test_insufficient_data_has_no_invented_statistics(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
    size: int,
) -> None:
    result = PriceEstimator(clock=lambda: pricing_now).estimate(
        product, [observation_factory(i) for i in range(size)]
    )
    assert result.status == EstimateStatus.INSUFFICIENT_DATA
    assert result.sample_size == size
    assert (result.estimated_price, result.median, result.q1, result.q3) == (None, None, None, None)
    assert result.confidence == Confidence.NONE and result.confidence_score == 0


def test_median_and_type7_quartiles_are_decimal(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    result = PriceEstimator(clock=lambda: pricing_now).estimate(
        product,
        [
            observation_factory(i, price)
            for i, price in enumerate(["90", "100", "110", "120", "130"])
        ],
    )
    assert result.status == EstimateStatus.OK
    assert (result.estimated_price, result.median, result.q1, result.q3) == (
        Decimal("110.00"),
        Decimal("110"),
        Decimal("100"),
        Decimal("120"),
    )
    assert result.sources == ("demo-market",)
    assert len(result.included_observations) == 5
    assert result.calculated_at == pricing_now
    assert result.model_dump(mode="json")["estimated_price"] == "110.00"
    assert quantile([Decimal("0.01"), Decimal("0.02")], Decimal("0.25")) == Decimal("0.0125")


@pytest.mark.parametrize(
    "prices",
    [
        ["100", "101", "102", "103", "104", "10000"],
        ["100", "100", "100", "100", "100", "10000"],
    ],
)
def test_iqr_removes_outliers_even_when_iqr_is_zero(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
    prices: list[str],
) -> None:
    result = PriceEstimator(clock=lambda: pricing_now).estimate(
        product, [observation_factory(i, price) for i, price in enumerate(prices)]
    )
    assert result.status == EstimateStatus.OK and result.sample_size == 5
    assert result.outlier_count == 1 and result.estimated_price <= Decimal("104")
    assert result.rejections[-1].reasons == ("iqr_outlier",)


def test_minimum_is_checked_after_outlier_filter(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    result = PriceEstimator(clock=lambda: pricing_now).estimate(
        product, [observation_factory(i, price) for i, price in enumerate(["100"] * 4 + ["10000"])]
    )
    assert result.status == EstimateStatus.INSUFFICIENT_DATA and result.sample_size == 4
    assert result.estimated_price is None
    assert result.confidence_reasons == ("sample_below_minimum_after_iqr",)


def test_sold_is_preferred_and_never_pooled_with_asking(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    sold = [observation_factory(i, "100") for i in range(5)]
    asking = [observation_factory(i + 10, "500", price_type=PriceType.ASKING) for i in range(20)]
    estimator = PriceEstimator(clock=lambda: pricing_now)
    result = estimator.estimate(product, sold + asking)
    assert result.price_basis == PriceBasis.SOLD
    assert result.estimated_price == Decimal("100") and result.sample_size == 5
    assert len(result.rejections) == 20
    insufficient = estimator.estimate(product, sold[:1] + asking)
    assert insufficient.status == EstimateStatus.INSUFFICIENT_DATA
    assert insufficient.price_basis == PriceBasis.SOLD and insufficient.sample_size == 1


def test_asking_only_is_explicit_and_confidence_capped(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    observations = [
        observation_factory(
            i,
            price_type=PriceType.ASKING,
            sale_confirmed=False,
            evidence_reference=None,
            listing_disappeared=True,
        )
        for i in range(30)
    ]
    result = PriceEstimator(clock=lambda: pricing_now).estimate(product, observations)
    assert result.price_basis == PriceBasis.ASKING_BASED and result.confidence == Confidence.LOW
    assert result.confidence_score <= 0.59
    assert "asking_prices_are_not_transactions" in result.confidence_reasons


@pytest.mark.parametrize(
    "overrides",
    [
        {"sale_confirmed": False, "listing_disappeared": True},
        {"evidence_reference": None},
        {"evidence_reference": "   "},
    ],
)
def test_sold_needs_confirmation_and_evidence(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
    overrides: dict[str, object],
) -> None:
    observations = [observation_factory(i, **overrides) for i in range(5)]
    result = PriceEstimator(clock=lambda: pricing_now).estimate(product, observations)
    assert result.status == EstimateStatus.INSUFFICIENT_DATA and result.sample_size == 0
    assert all("unconfirmed_sale" in rejection.reasons for rejection in result.rejections)


def test_mixed_currencies_do_not_mix_prices(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    observations = [observation_factory(i, "100") for i in range(5)]
    observations += [observation_factory(i + 10, "999", currency="EUR") for i in range(10)]
    result = PriceEstimator(clock=lambda: pricing_now).estimate(product, observations)
    assert result.sample_size == 5 and result.currency == "PLN"
    assert result.estimated_price == Decimal("100")
    assert all(rejection.reasons == ("currency_mismatch",) for rejection in result.rejections)


@pytest.mark.parametrize(
    "reason",
    ["unreliable_source", "future_observation", "stale_observation", "provenance_mismatch"],
)
def test_unreliable_evidence_is_excluded(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
    reason: str,
) -> None:
    overrides = {
        "unreliable_source": {"source_reliability": 0.1},
        "future_observation": {"observed_at": pricing_now + timedelta(days=1)},
        "stale_observation": {"observed_at": pricing_now - timedelta(days=91)},
        "provenance_mismatch": {"is_demo": False},
    }
    result = PriceEstimator(clock=lambda: pricing_now).estimate(
        product, [observation_factory(1, **overrides[reason])]
    )
    assert result.sample_size == 0 and reason in result.rejections[0].reasons


def test_many_correct_observations_are_high_confidence(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    observations = [observation_factory(i, str(98 + i % 5)) for i in range(1000)]
    result = PriceEstimator(clock=lambda: pricing_now).estimate(product, observations)
    assert result.sample_size == 1000 and result.estimated_price == Decimal("100")
    assert result.confidence == Confidence.HIGH


def test_duplicate_observations_do_not_inflate_sample(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    observations = [observation_factory(i) for i in range(5)]
    estimator = PriceEstimator(clock=lambda: pricing_now)
    result = estimator.estimate(product, observations + [observations[0]] * 50)
    assert result.sample_size == 5
    assert result.rejections[0].reasons == ("duplicate_observation",)
    conflicting = estimator.estimate(product, observations + [observation_factory(0, "999")])
    assert conflicting.sample_size == 4 and conflicting.estimated_price is None
    assert conflicting.rejections[0].reasons == ("conflicting_observations",)


def test_self_observation_is_excluded(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    target = PricingProduct.model_validate(
        {**product.model_dump(), "source_name": "demo-market", "external_id": "DEMO-0"}
    )
    result = PriceEstimator(clock=lambda: pricing_now).estimate(
        target, [observation_factory(i) for i in range(5)]
    )
    assert result.sample_size == 4
    assert result.rejections[0].reasons == ("self_observation",)


def test_configurable_minimum_and_decimal_rounding(
    product: PricingProduct,
    pricing_now: datetime,
    observation_factory: Callable[..., PriceObservation],
) -> None:
    estimator = PriceEstimator(PricingConfig(min_observations=2), clock=lambda: pricing_now)
    result = estimator.estimate(
        product, [observation_factory(0, "0.01"), observation_factory(1, "0.02")]
    )
    assert result.median == Decimal("0.015") and result.estimated_price == Decimal("0.02")
    assert result.q1 == Decimal("0.0125") and result.q3 == Decimal("0.0175")
