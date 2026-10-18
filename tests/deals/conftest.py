from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.price_estimator import PriceEstimator
from app.schemas.deals import (
    AnalysisRequest,
    AvailableObservation,
    CostBreakdown,
    DealSnapshot,
    DemandEvidence,
)
from app.schemas.pricing import (
    MarketEstimate,
    PriceObservation,
    PriceType,
    PricingProduct,
    ProductFeatures,
)
from vinted_deal_hunter.models import Condition, OfferInput


@pytest.fixture
def deal_now() -> datetime:
    return datetime(2026, 1, 10, 12, tzinfo=UTC)


@pytest.fixture
def snapshot(deal_now: datetime) -> DealSnapshot:
    offer = OfferInput(
        source_name="demo",
        external_id="DEMO-target",
        product_name="DEMO sneaker",
        brand="DEMO brand",
        size="EU 42",
        condition=Condition.GOOD,
        asking_price=Decimal(40),
        currency="PLN",
        is_demo=True,
        published_at=deal_now - timedelta(hours=1),
    )
    return DealSnapshot(
        offer=offer,
        available_at=deal_now,
        model="DEMO model",
        category="sneakers",
        description="Fictional DEMO listing",
        photo_count=3,
        costs=CostBreakdown(
            currency="PLN",
            buyer_fees=Decimal(2),
            inbound_shipping=Decimal(5),
            resale_fees=Decimal(2),
            outbound_shipping_subsidy=Decimal(1),
            other_costs=Decimal(0),
        ),
        demand=DemandEvidence(
            score=Decimal("0.8"),
            source_reference="DEMO demand",
            observed_at=deal_now,
            available_at=deal_now,
        ),
    )


@pytest.fixture
def request_data(snapshot: DealSnapshot, deal_now: datetime) -> AnalysisRequest:
    observations = tuple(
        AvailableObservation(
            observation=PriceObservation(
                source_name="demo-market",
                external_id=f"DEMO-comparable-{i}",
                features=ProductFeatures.from_offer(snapshot.offer, model=snapshot.model),
                price=Decimal(200),
                currency="PLN",
                price_type=PriceType.SOLD,
                observed_at=deal_now - timedelta(days=1),
                source_reliability=0.95,
                is_demo=True,
                sale_confirmed=True,
                evidence_reference=f"DEMO receipt {i}",
            ),
            available_at=deal_now - timedelta(days=1),
        )
        for i in range(20)
    )
    return AnalysisRequest(snapshot=snapshot, observations=observations, as_of=deal_now)


@pytest.fixture
def estimate(request_data: AnalysisRequest) -> MarketEstimate:
    return PriceEstimator(clock=lambda: request_data.as_of).estimate(
        PricingProduct.from_offer(request_data.snapshot.offer, model=request_data.snapshot.model),
        [item.observation for item in request_data.observations],
    )
