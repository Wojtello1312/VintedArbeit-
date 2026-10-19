"""Composition root for the API lifespan. Secrets stay outside serialized policy."""

import os

from app.admin.store import AdminStore
from app.ai.reviewer import AIReviewer, ReviewedDealPipeline
from app.core.deal_pipeline import DealPipeline
from app.notifications.formatting import destination_key
from app.notifications.outbox import AlertOutbox
from app.notifications.service import NotificationService
from app.notifications.telegram import TelegramNotifier
from app.scheduler.cycle import AnalysisContextProvider, DealCycle
from app.scheduler.service import JobManager
from app.scheduler.store import JobStore
from app.storage.sqlite import SQLiteAnalysisStore
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import OfferRepository
from vinted_deal_hunter.sources import OfferSource
from vinted_deal_hunter.vinted_playwright import configured_vinted_source


class JobRuntime:
    def __init__(
        self,
        settings: Settings,
        repository: OfferRepository,
        *,
        source: OfferSource | None = None,
        context: AnalysisContextProvider | None = None,
    ) -> None:
        self.base_settings = settings
        self.panel = AdminStore(settings.jobs_db_path)
        self.policy_revision, policy = self.panel.policy()
        if policy is not None:
            settings = policy.apply(settings)
        self.settings = settings
        self.analysis_store = SQLiteAnalysisStore(settings.db_path.with_name("deals.sqlite3"))
        pipeline = DealPipeline(self.analysis_store, settings.deal, settings.pricing)
        self.reviewer = AIReviewer(settings.ai, api_key=os.environ.get("OPENAI_API_KEY"))
        processor: DealPipeline | ReviewedDealPipeline = pipeline
        if settings.deal.ai_review_required:
            processor = ReviewedDealPipeline(pipeline, self.reviewer)
        self.telegram = TelegramNotifier(
            settings.notifications, settings.telegram_bot_token, settings.chat_id
        )
        destination = (
            destination_key(settings.chat_id.get_secret_value()) if settings.chat_id else ""
        )
        outbox = AlertOutbox(settings.jobs_db_path, settings.notifications)
        notifications = NotificationService(outbox, pipeline, self.telegram, destination)
        selected_source = (
            source if source is not None else configured_vinted_source(settings.vinted)
        )
        cycle = DealCycle(
            settings, repository, processor, notifications, source=selected_source, context=context
        )
        self.manager = JobManager(cycle, JobStore(settings.jobs_db_path), settings.scheduler)
        self.manager.before_run = self.refresh_policy

    def refresh_policy(self) -> None:
        """Caller holds the cycle lock; apply changes only between analyses."""
        revision, policy = self.panel.policy()
        if revision == self.policy_revision or policy is None:
            return
        settings = policy.apply(self.base_settings)
        pipeline = DealPipeline(self.analysis_store, settings.deal, settings.pricing)
        self.reviewer.close()
        self.reviewer = AIReviewer(settings.ai, api_key=os.environ.get("OPENAI_API_KEY"))
        processor: DealPipeline | ReviewedDealPipeline = pipeline
        if settings.deal.ai_review_required:
            processor = ReviewedDealPipeline(pipeline, self.reviewer)
        self.manager.cycle.settings = settings
        self.manager.cycle.pipeline = processor
        self.manager.cycle.notifications.pipeline = pipeline
        self.manager.reconfigure(settings.scheduler)
        self.settings, self.policy_revision = settings, revision

    async def close(self) -> None:
        try:
            await self.manager.stop()
        finally:
            try:
                await self.telegram.close()
            finally:
                self.reviewer.close()
                self.analysis_store.close()
