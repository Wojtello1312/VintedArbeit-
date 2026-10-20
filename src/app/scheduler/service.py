"""Manual and scheduled runs share a cross-process lock and durable idempotency keys."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from app.core.price_estimator import now_utc
from app.scheduler.config import SchedulerConfig
from app.scheduler.cycle import DealCycle
from app.scheduler.store import CycleReport, JobStore

logger = logging.getLogger(__name__)


class JobBusy(RuntimeError):
    pass


class JobManager:
    def __init__(
        self,
        cycle: DealCycle,
        store: JobStore,
        config: SchedulerConfig,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.cycle, self.store, self.config, self.clock = cycle, store, config, clock
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._task: asyncio.Task[CycleReport] | None = None
        self._closing = False
        self.before_run: Callable[[], None] | None = None
        self.cycle.progress = self.store.save

    def reconfigure(self, config: SchedulerConfig) -> None:
        self.config = config
        if not self.scheduler.running:
            if config.enabled:
                self.start()
            return
        job = self.scheduler.get_job("deal-cycle")
        if not config.enabled:
            if job is not None:
                self.scheduler.remove_job("deal-cycle")
        elif job is not None:
            self.scheduler.reschedule_job(
                "deal-cycle", trigger="interval", seconds=config.interval_seconds
            )
            self.scheduler.modify_job("deal-cycle", misfire_grace_time=config.interval_seconds)
        else:
            self.scheduler.add_job(
                self.scheduled_run,
                "interval",
                seconds=config.interval_seconds,
                id="deal-cycle",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=config.interval_seconds,
            )

    def start(self) -> None:
        if self.config.enabled:
            self.scheduler.add_job(
                self.scheduled_run,
                "interval",
                seconds=self.config.interval_seconds,
                id="deal-cycle",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=self.config.interval_seconds,
            )
            self.scheduler.start()

    async def stop(self) -> None:
        self._closing = True
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self._task is not None:
            # A cancelled request must not release the lock while its worker thread continues.
            await asyncio.shield(self._task)

    async def scheduled_run(self) -> None:
        if self._closing or await asyncio.to_thread(self.store.paused):
            return
        try:
            await self.run()
        except JobBusy:
            logger.info("Scheduled cycle skipped: another cycle is running")
        except Exception as exc:
            logger.error("Scheduled cycle failed error=%s", type(exc).__name__)

    async def run(self, run_id: str | None = None) -> CycleReport:
        return await asyncio.shield(await self.submit(run_id))

    async def submit(self, run_id: str | None = None) -> asyncio.Task[CycleReport]:
        """Acquire the lease before returning; the UI can immediately poll progress."""
        identity = run_id or uuid4().hex
        previous = await asyncio.to_thread(self.store.get, identity)
        if previous is not None and previous.status != "running":

            async def completed() -> CycleReport:
                return previous

            return asyncio.create_task(completed())
        if self._closing or (self._task is not None and not self._task.done()):
            raise JobBusy("cycle is already running or shutting down")
        lease = self.store.acquire()
        if lease is None:
            raise JobBusy("cycle is running in another worker")

        # No await between lock acquisition and owning task assignment.
        async def execute() -> CycleReport:
            try:
                if self.before_run is not None:
                    self.before_run()
                await asyncio.to_thread(self.store.recover_interrupted, self.clock())
                cached = await asyncio.to_thread(self.store.get, identity)
                if cached is not None:
                    return cached
                return await self._execute(identity)
            finally:
                lease.close()

        self._task = asyncio.create_task(execute())
        return self._task

    async def _execute(self, identity: str) -> CycleReport:
        started = time.monotonic()
        report = CycleReport(
            run_id=identity,
            status="running",
            started_at=self.clock(),
            source_name=self.cycle.source.source_name,
            is_demo=self.cycle.source.is_demo,
        )
        await asyncio.to_thread(self.store.save, report)
        try:
            report = await asyncio.to_thread(self.cycle.process, report)
            report = report.model_copy(update={"phase": "notifying"})
            await asyncio.to_thread(self.store.save, report)
            deliveries = await self.cycle.notifications.flush(
                allow_production=not self.cycle.source.is_demo
            )
            alerts = dict(report.alerts)
            alerts.update({f"delivery_{key}": value for key, value in deliveries.items()})
            report = report.model_copy(
                update={"status": "completed", "phase": "completed", "alerts": alerts}
            )
        except Exception as exc:
            logger.error("Cycle failed id=%s error=%s", identity, type(exc).__name__)
            saved_report = await asyncio.to_thread(self.store.get, identity)
            if saved_report is not None:
                report = saved_report
            report = report.model_copy(
                update={"status": "error", "phase": "error", "error_type": type(exc).__name__}
            )
        report = report.model_copy(
            update={"finished_at": self.clock(), "duration_seconds": time.monotonic() - started}
        )
        await asyncio.to_thread(self.store.save, report)
        logger.info(
            "Cycle finished id=%s status=%s processed=%d errors=%d duration_seconds=%.3f",
            identity,
            report.status,
            report.processed,
            report.import_errors + report.analysis_errors,
            report.duration_seconds,
        )
        return report

    async def status(self) -> dict[str, object]:
        paused = await asyncio.to_thread(self.store.paused)
        latest = await asyncio.to_thread(self.store.latest)
        job = self.scheduler.get_job("deal-cycle") if self.scheduler.running else None
        next_run = job.next_run_time if job is not None and not paused else None
        return {
            "enabled": self.config.enabled,
            "paused": paused,
            "running": await asyncio.to_thread(self.store.is_running),
            "interval_seconds": self.config.interval_seconds,
            "next_run_at": next_run,
            "source_name": self.cycle.source.source_name,
            "is_demo": self.cycle.source.is_demo,
            "last_run": latest,
            "totals": await asyncio.to_thread(self.store.totals),
            "notifications": await asyncio.to_thread(self.cycle.notifications.outbox.counts),
            "telegram_ready": self.cycle.notifications.telegram.ready,
            "daily_message_limit": self.cycle.notifications.outbox.config.daily_limit,
            "daily_reserved_or_sent": await asyncio.to_thread(
                self.cycle.notifications.outbox.daily_count, None, self.clock()
            ),
        }

    async def pause(self) -> None:
        await asyncio.to_thread(self.store.set_paused, True)

    async def resume(self) -> None:
        await asyncio.to_thread(self.store.set_paused, False)
