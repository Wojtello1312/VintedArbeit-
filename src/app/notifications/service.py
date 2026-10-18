import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from app.core.deal_pipeline import DealPipeline
from app.core.price_estimator import now_utc
from app.notifications.formatting import make_alert
from app.notifications.outbox import AlertOutbox
from app.notifications.telegram import DeliveryResult, DemoNotifier, Notifier
from app.schemas.deals import AnalysisRecord

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(
        self,
        outbox: AlertOutbox,
        pipeline: DealPipeline,
        telegram: Notifier,
        destination: str,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.outbox, self.pipeline, self.telegram = outbox, pipeline, telegram
        self.destination = destination
        self.clock = clock
        self.demo = DemoNotifier()

    def enqueue(self, record: AnalysisRecord) -> str:
        try:
            message = make_alert(record, self.destination)
        except ValueError:
            logger.info(
                "Alert skipped analysis=%s reason=incomplete_alert_data", record.analysis_id
            )
            return "skipped"
        return "enqueued" if self.outbox.enqueue(message, self.clock()) else "duplicate"

    async def flush(self, *, allow_production: bool = True) -> dict[str, int]:
        await asyncio.to_thread(self.outbox.recover, self.clock())
        counts: dict[str, int] = {}
        for message in await asyncio.to_thread(
            self.outbox.pending, self.clock(), self.destination, allow_production
        ):
            notifier: Notifier = self.demo if message.is_demo else self.telegram
            if not notifier.ready or (
                not message.is_demo and message.destination != self.destination
            ):
                counts["disabled"] = counts.get("disabled", 0) + 1
                continue
            claim = await asyncio.to_thread(self.outbox.claim, message.alert_key, self.clock())
            if claim is None:
                counts["not_claimed"] = counts.get("not_claimed", 0) + 1
                continue
            try:
                result = await asyncio.wait_for(
                    notifier.send(message), timeout=self.outbox.config.timeout_seconds + 1
                )
            except asyncio.CancelledError:
                # Durable 'sending' is recovered as uncertain; never resend after cancellation.
                raise
            except Exception as exc:
                logger.error(
                    "Alert attempt uncertain key=%s error=%s", message.alert_key, type(exc).__name__
                )
                result = DeliveryResult(status="uncertain", reason="delivery_unknown")
            status = await asyncio.to_thread(self.outbox.complete, claim, result, self.clock())
            status = status or "uncommitted"
            counts[status] = counts.get(status, 0) + 1
            logger.info(
                "Alert attempt key=%s status=%s reason=%s",
                message.alert_key,
                status,
                result.reason,
            )
        for sent in await asyncio.to_thread(self.outbox.unreconciled):
            try:
                await asyncio.to_thread(
                    self.pipeline.mark_notified, sent.message.analysis_id, sent.receipt
                )
                await asyncio.to_thread(self.outbox.reconciled, sent.message.alert_key)
            except Exception as exc:
                logger.error(
                    "Alert checkpoint pending key=%s error=%s",
                    sent.message.alert_key,
                    type(exc).__name__,
                )
                counts["checkpoint_pending"] = counts.get("checkpoint_pending", 0) + 1
        return counts
