"""Source ingestion -> context -> deterministic/optional AI -> durable notification outbox."""

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.ai.reviewer import ReviewedDealPipeline
from app.core.deal_pipeline import DealPipeline
from app.notifications.service import NotificationService
from app.scheduler.store import CycleReport
from app.schemas.deals import AnalysisRequest, CostBreakdown, DealSnapshot, DealState
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import Disposition, OfferRepository
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.models import OfferInput
from vinted_deal_hunter.sources import MockOfferSource, OfferSource

logger = logging.getLogger(__name__)


class AnalysisContextProvider(Protocol):
    def prepare(self, offer: OfferInput, as_of: datetime) -> AnalysisRequest: ...


class EmptyContextProvider:
    """Unknown costs and absent comparisons stay unknown, including in production."""

    def prepare(self, offer: OfferInput, as_of: datetime) -> AnalysisRequest:
        return AnalysisRequest(
            snapshot=DealSnapshot(
                offer=offer, available_at=as_of, costs=CostBreakdown(currency=offer.currency)
            ),
            as_of=as_of,
        )


class CapturingRepository:
    def __init__(self, repository: OfferRepository) -> None:
        self.repository = repository
        self.offers: dict[tuple[str, str], OfferInput] = {}
        self.conflicts: set[tuple[str, str]] = set()

    def add_many(self, offers: Sequence[OfferInput]) -> list[Disposition]:
        results = self.repository.add_many(offers)
        for offer, disposition in zip(offers, results, strict=True):
            if disposition == Disposition.PROVENANCE_CONFLICT:
                continue
            key = (offer.source_name, offer.external_id)
            if key in self.offers and self.offers[key] != offer:
                self.conflicts.add(key)
            self.offers[key] = offer
        return results


class DealCycle:
    def __init__(
        self,
        settings: Settings,
        repository: OfferRepository,
        pipeline: DealPipeline | ReviewedDealPipeline,
        notifications: NotificationService,
        *,
        source: OfferSource | None = None,
        context: AnalysisContextProvider | None = None,
    ) -> None:
        self.settings, self.repository = settings, repository
        self.pipeline, self.notifications = pipeline, notifications
        self.source = source if source is not None else MockOfferSource()
        self.context = context if context is not None else EmptyContextProvider()

    def process(self, report: CycleReport) -> CycleReport:
        captured = CapturingRepository(self.repository)
        imported = import_offers(self.source, captured, self.settings)
        states: Counter[str] = Counter()
        alerts: Counter[str] = Counter()
        errors = len(captured.conflicts)
        for key, offer in captured.offers.items():
            if key in captured.conflicts:
                continue
            try:
                request = self.context.prepare(offer, report.started_at)
                if request.snapshot.offer != offer or request.as_of != report.started_at:
                    raise ValueError("context must preserve source identity, data and cycle time")
                # Demo sources never invoke AI, even if an explicitly enabled reviewer is injected.
                processor = self.pipeline
                if offer.is_demo and isinstance(processor, ReviewedDealPipeline):
                    record = processor.pipeline.process(request)
                    if record.state == DealState.AI_PENDING:
                        from app.ai.schemas import ReviewOutcome

                        record = processor.pipeline.complete_ai_review(
                            record.analysis_id,
                            ReviewOutcome(status="skipped", reason="scheduler_demo_offline"),
                        )
                else:
                    record = processor.process(request)
                states[record.state.value] += 1
                if record.state == DealState.APPROVED:
                    alerts[self.notifications.enqueue(record)] += 1
            except Exception as exc:
                errors += 1
                logger.error(
                    "Cycle item failed source=%s error=%s", offer.source_name, type(exc).__name__
                )
        return report.model_copy(
            update={
                "fetched": imported.total,
                "imported": imported.new,
                "skipped": imported.skipped,
                "import_errors": imported.errors,
                "processed": sum(states.values()),
                "analysis_errors": errors + states.get(DealState.ERROR.value, 0),
                "states": dict(states),
                "alerts": dict(alerts),
            }
        )
