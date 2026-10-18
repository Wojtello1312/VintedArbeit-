"""Explicit historical DEMO scenario: fictional prices and evidence, no external calls."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.resources import files

from app.ai.schemas import ReviewOutcome
from app.core.deal_pipeline import DealPipeline
from app.migrate import upgrade
from app.notifications.outbox import AlertOutbox
from app.notifications.service import NotificationService
from app.notifications.telegram import DemoNotifier
from app.schemas.deals import (
    AnalysisRequest,
    AvailableObservation,
    CostBreakdown,
    DealSnapshot,
    DealState,
    DemandEvidence,
)
from app.schemas.pricing import PriceObservation, PriceType, ProductFeatures
from app.storage.sqlite import SQLiteAnalysisStore
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.models import Condition, OfferInput
from vinted_deal_hunter.sources import JSONOfferSource

DEMO_TIME = datetime(2026, 1, 10, 12, tzinfo=UTC)
SOURCE = "demo-readiness"


def demo_content() -> bytes:
    return files("app").joinpath("data/readiness_demo.json").read_bytes()


def demo_source() -> JSONOfferSource:
    return JSONOfferSource(demo_content(), source_name=SOURCE, is_demo=True)


def demo_request(offer: OfferInput) -> AnalysisRequest:
    if not offer.is_demo or offer.source_name != SOURCE:
        raise ValueError("Fictional context is only available for the explicit readiness DEMO")
    features = ProductFeatures(
        product_name="DEMO Runner",
        brand="demo brand",
        model="DEMO R1",
        size="EU 42",
        condition=Condition.GOOD,
    )
    observations = tuple(
        AvailableObservation(
            observation=PriceObservation(
                source_name="demo-market",
                external_id=f"DEMO-sale-{i:02}",
                features=features,
                price=Decimal(190 + i),
                currency="PLN",
                price_type=PriceType.SOLD,
                observed_at=DEMO_TIME - timedelta(days=1),
                source_reliability=0.95,
                is_demo=True,
                sale_confirmed=True,
                evidence_reference=f"DEMO fictional receipt {i}",
            ),
            available_at=DEMO_TIME - timedelta(days=1),
        )
        for i in range(20)
    )
    return AnalysisRequest(
        snapshot=DealSnapshot(
            offer=offer,
            available_at=DEMO_TIME,
            model="DEMO R1",
            category="sneakers",
            description="DEMO — fikcyjny produkt i fikcyjne transakcje porównawcze.",
            photo_count=3,
            listing_url=f"https://example.com/DEMO/{offer.external_id}",
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
                source_reference="DEMO fictional demand",
                observed_at=DEMO_TIME,
                available_at=DEMO_TIME,
            ),
        ),
        observations=observations,
        as_of=DEMO_TIME,
    )


def main() -> None:
    settings = Settings.from_env()
    upgrade(settings)
    repository = SQLiteOfferRepository(settings.db_path)
    store = SQLiteAnalysisStore(settings.db_path.with_name("deals.sqlite3"))
    try:
        report = import_offers(demo_source(), repository, settings)
        pipeline = DealPipeline(store, settings.deal, settings.pricing)
        notifications = NotificationService(
            AlertOutbox(settings.jobs_db_path, settings.notifications),
            pipeline,
            DemoNotifier(),
            "demo",
        )
        states: dict[str, int] = {}
        for offer in repository.list_offers():
            if offer.source_name != SOURCE or not offer.is_demo:
                continue
            record = pipeline.process(demo_request(offer))
            if record.state == DealState.AI_PENDING:
                record = pipeline.complete_ai_review(
                    record.analysis_id, ReviewOutcome(status="skipped", reason="demo_offline")
                )
            if record.state == DealState.APPROVED:
                notifications.enqueue(record)
            states[record.state.value] = states.get(record.state.value, 0) + 1
        deliveries: dict[str, int] = {}
        for _ in range(
            (report.total + settings.notifications.batch_size - 1)
            // settings.notifications.batch_size
        ):
            batch = asyncio.run(notifications.flush(allow_production=False))
            for status, count in batch.items():
                deliveries[status] = deliveries.get(status, 0) + count
        print(
            json.dumps(
                {
                    "demo": True,
                    "import": report.model_dump(mode="json"),
                    "states_before_delivery": states,
                    "local_deliveries": deliveries,
                }
            )
        )
    finally:
        repository.close()
        store.close()


if __name__ == "__main__":
    main()
