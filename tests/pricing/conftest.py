from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.schemas.pricing import PriceObservation, PriceType, PricingProduct, ProductFeatures
from vinted_deal_hunter.models import Condition


@pytest.fixture
def pricing_now() -> datetime:
    return datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.fixture
def features() -> ProductFeatures:
    return ProductFeatures(
        product_name="Nike Air Max 90",
        brand="Nike",
        model="Air Max 90",
        size="EU 42",
        condition=Condition.VERY_GOOD,
    )


@pytest.fixture
def product(features: ProductFeatures) -> PricingProduct:
    return PricingProduct(
        product_id="DEMO-PRODUCT", features=features, currency="PLN", is_demo=True
    )


@pytest.fixture
def observation_factory(
    features: ProductFeatures,
    pricing_now: datetime,
) -> Callable[..., PriceObservation]:
    def make(index: int, price: str = "100.00", **overrides: Any) -> PriceObservation:
        return PriceObservation.model_validate(
            {
                "source_name": "demo-market",
                "external_id": f"DEMO-{index}",
                "features": features,
                "price": price,
                "currency": "PLN",
                "price_type": PriceType.SOLD,
                "observed_at": pricing_now,
                "source_reliability": 0.95,
                "is_demo": True,
                "sale_confirmed": True,
                "evidence_reference": f"demo-receipt-{index}",
                **overrides,
            }
        )

    return make
