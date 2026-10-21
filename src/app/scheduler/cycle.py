"""Source ingestion -> context -> deterministic/optional AI -> durable notification outbox."""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from app.ai.reviewer import ReviewedDealPipeline
from app.core.deal_pipeline import DealPipeline
from app.notifications.service import NotificationService
from app.scheduler.store import CycleReport
from app.schemas.deals import AnalysisRequest, CostBreakdown, DealSnapshot, DealState
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import Disposition, OfferRepository
from vinted_deal_hunter.ingestion import ImportReport, PartialImportError, import_offers
from vinted_deal_hunter.models import OfferInput
from vinted_deal_hunter.sources import MockOfferSource, OfferSource
from vinted_deal_hunter.vinted import CollectionReport, VintedOfferSource
from vinted_deal_hunter.vinted_playwright import VintedPlaywrightOfferSource

logger = logging.getLogger(__name__)


class AnalysisContextProvider(Protocol):
    def prepare(self, offer: OfferInput, as_of: datetime) -> AnalysisRequest: ...


class EmptyContextProvider:
    """Unknown costs and absent comparisons stay unknown, including in production."""

    def prepare(self, offer: OfferInput, as_of: datetime) -> AnalysisRequest:
        return AnalysisRequest(
            snapshot=DealSnapshot(
                offer=offer,
                available_at=as_of,
                costs=CostBreakdown(currency=offer.currency),
                listing_url=offer.listing_url,
                category=offer.category,
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
            if disposition in {Disposition.PROVENANCE_CONFLICT, Disposition.DELETED}:
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
        self.progress: Callable[[CycleReport], None] = lambda _: None

    def process(self, report: CycleReport) -> CycleReport:
        def import_progress(imported: ImportReport) -> None:
            nonlocal report
            report = report.model_copy(
                update={
                    "fetched": imported.total,
                    "imported": imported.new,
                    "skipped": imported.skipped,
                    "import_errors": imported.errors,
                }
            )
            self.progress(report)

        def collection_progress(collection: CollectionReport) -> None:
            nonlocal report
            reasons = Counter(page.reason for page in collection.pages if page.status == "excluded")
            report = report.model_copy(
                update={
                    "pages": len(collection.pages),
                    "fetched": sum(page.status == "collected" for page in collection.pages),
                    "excluded": sum(page.status == "excluded" for page in collection.pages),
                    "collection_errors": sum(page.status == "error" for page in collection.pages),
                    "requests": collection.requests,
                    "collection_end_reason": collection.end_reason,
                    "exclusion_reasons": {key: value for key, value in reasons.items() if key},
                }
            )
            self.progress(report)

        collector = (
            self.source
            if isinstance(self.source, (VintedOfferSource, VintedPlaywrightOfferSource))
            else None
        )
        if collector is not None:
            collector.progress = collection_progress
            report = report.model_copy(
                update={
                    "max_requests": collector.config.max_requests,
                    "max_pages": collector.config.max_pages,
                }
            )
        captured = CapturingRepository(self.repository)
        try:
            imported = import_offers(self.source, captured, self.settings, progress=import_progress)
        except PartialImportError as exc:
            imported = exc.report
            report = report.model_copy(
                update={
                    "collection_incomplete": True,
                    "error_type": exc.original_error_type,
                    "error_code": exc.code,
                }
            )
            logger.warning(
                "Collection interrupted id=%s error=%s code=%s imported=%d skipped=%d",
                report.run_id,
                exc.original_error_type,
                exc.code or "unknown",
                imported.new,
                imported.skipped,
            )
        finally:
            if collector is not None:
                collection_progress(collector.report)
        report = report.model_copy(
            update={
                "phase": "analyzing",
                "fetched": imported.total,
                "imported": imported.new,
                "skipped": imported.skipped,
                "import_errors": imported.errors,
            }
        )
        self.progress(report)
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
            self.progress(
                report.model_copy(
                    update={
                        "processed": sum(states.values()),
                        "analysis_errors": errors + states.get(DealState.ERROR.value, 0),
                    }
                )
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
