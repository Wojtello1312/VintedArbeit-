import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.ai.config import AIConfig
from app.deal_config import DealConfig
from app.scheduler.config import SchedulerConfig
from app.scheduler.runtime import JobRuntime
from app.scheduler.service import JobBusy, JobManager
from app.scheduler.store import JobStore
from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.sources import JSONOfferSource, MockOfferSource


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/jobs/status"),
        ("post", "/jobs/run"),
        ("post", "/jobs/pause"),
        ("post", "/jobs/resume"),
    ],
)
def test_authentication_required(method, path, settings, auth):
    with TestClient(create_app(settings)) as client:
        request = getattr(client, method)
        assert request(path).status_code == 401
        assert request(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert request(path, headers={"Authorization": "Basic abc"}).status_code == 401
        assert (
            request(path, params={"token": settings.jobs_api_token.get_secret_value()}).status_code
            == 401
        )
        assert request(path, headers=auth).status_code == 200


def test_missing_auth_configuration_fails_closed(settings):
    config = settings.model_copy(update={"jobs_api_token": None})
    with TestClient(create_app(config)) as client:
        assert client.get("/jobs/status").status_code == 503
        for action in ("run", "pause", "resume"):
            assert client.post("/jobs/" + action).status_code == 503


def test_demo_run_counts_job_idempotence_and_repeated_offers(settings, auth):
    with TestClient(create_app(settings)) as client:
        initial = client.get("/jobs/status", headers=auth).json()
        assert initial["source_name"] == "demo" and initial["is_demo"] is True
        assert initial["running"] is False and initial["next_run_at"] is not None
        first = client.post("/jobs/run", headers=auth, json={"run_id": "same-job"}).json()
        assert first["status"] == "completed"
        assert (first["fetched"], first["imported"], first["skipped"], first["import_errors"]) == (
            130,
            110,
            10,
            10,
        )
        assert first["processed"] == 110 and first["analysis_errors"] == 0
        assert first["duration_seconds"] >= 0
        assert first["alerts"] == {}  # Unknown costs/comparables are never fabricated.
        assert client.post("/jobs/run", headers=auth, json={"run_id": "same-job"}).json() == first
        second = client.post("/jobs/run", headers=auth, json={"run_id": "next-job"}).json()
        assert (second["imported"], second["skipped"], second["processed"]) == (0, 120, 110)
        status = client.get("/jobs/status", headers=auth).json()
        assert status["totals"] == {"cycles": 2, "processed": 220, "failed_cycles": 0}
        assert status["notifications"] == {}
        assert settings.jobs_api_token.get_secret_value() not in json.dumps(status)
    with TestClient(create_app(settings)) as client:
        assert client.post("/jobs/run", headers=auth, json={"run_id": "same-job"}).json() == first
        assert client.get("/jobs/status", headers=auth).json()["totals"]["cycles"] == 2


def test_pause_resume_persists_and_manual_run_still_allowed(settings, auth):
    with TestClient(create_app(settings)) as client:
        paused = client.post("/jobs/pause", headers=auth).json()
        assert paused["paused"] is True and paused["next_run_at"] is None
        assert client.post("/jobs/run", headers=auth).json()["processed"] == 110
    with TestClient(create_app(settings)) as client:
        assert client.get("/jobs/status", headers=auth).json()["paused"] is True
        resumed = client.post("/jobs/resume", headers=auth).json()
        assert resumed["paused"] is False and resumed["next_run_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"run_id": "../escape"},
        {"run_id": "x" * 129},
        {"run_id": ""},
        {"run_id": "okay", "source": "vinted"},
    ],
)
def test_run_input_is_bounded(payload, settings, auth):
    with TestClient(create_app(settings)) as client:
        assert client.post("/jobs/run", headers=auth, json=payload).status_code == 422


def test_manual_runs_conflict_across_managers_and_pause_gates_schedule(settings):
    repository = SQLiteOfferRepository(settings.db_path)
    runtime = JobRuntime(settings, repository)
    second = JobManager(runtime.manager.cycle, JobStore(settings.jobs_db_path), settings.scheduler)
    entered, release = threading.Event(), threading.Event()
    original = runtime.manager.cycle.process

    def slow(report):
        entered.set()
        assert release.wait(5)
        return original(report)

    runtime.manager.cycle.process = slow

    async def run():
        first = asyncio.create_task(runtime.manager.run("first"))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            assert (await runtime.manager.status())["running"] is True
            with pytest.raises(JobBusy):
                await runtime.manager.run("first")
            with pytest.raises(JobBusy):
                await second.run("other")
        finally:
            release.set()
        await first
        await second.pause()
        await runtime.manager.scheduled_run()
        assert runtime.manager.store.totals()["cycles"] == 1
        await runtime.close()

    asyncio.run(run())
    repository.close()


def test_cancelled_http_waiter_does_not_release_running_cycle(settings):
    repository = SQLiteOfferRepository(settings.db_path)
    runtime = JobRuntime(settings, repository)
    entered, release = threading.Event(), threading.Event()
    original = runtime.manager.cycle.process

    def slow(report):
        entered.set()
        assert release.wait(5)
        return original(report)

    runtime.manager.cycle.process = slow

    async def run():
        waiter = asyncio.create_task(runtime.manager.run("cancelled-request"))
        assert await asyncio.to_thread(entered.wait, 5)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        try:
            with pytest.raises(JobBusy):
                await runtime.manager.run("another-request")
        finally:
            release.set()
        await runtime.close()  # Waits for the shielded cycle to finish.
        assert runtime.manager.store.get("cancelled-request").status == "completed"
        assert not runtime.manager.store.is_running()

    asyncio.run(run())
    repository.close()


def test_apscheduler_actually_executes_interval_and_shutdown(settings):
    repository = SQLiteOfferRepository(settings.db_path)
    config = settings.model_copy(update={"scheduler": SchedulerConfig(interval_seconds=1)})
    runtime = JobRuntime(config, repository)

    async def run():
        called = asyncio.Event()

        async def tick():
            called.set()

        runtime.manager.scheduled_run = tick
        runtime.manager.start()
        job = runtime.manager.scheduler.get_job("deal-cycle")
        assert job.max_instances == 1 and job.coalesce is True
        await asyncio.wait_for(called.wait(), timeout=3)
        await runtime.close()
        await asyncio.sleep(0)
        assert not runtime.manager.scheduler.running

    asyncio.run(run())
    repository.close()


def test_source_failure_reported_and_lock_released(settings, auth):
    source = MockOfferSource()
    source.read = Mock(side_effect=RuntimeError("secret remote token"))
    with TestClient(create_app(settings, source=source)) as client:
        response = client.post("/jobs/run", headers=auth, json={"run_id": "failed-run"})
        assert response.status_code == 200
        report = response.json()
        assert report["status"] == "error" and report["error_type"] == "RuntimeError"
        assert "secret remote token" not in response.text
        assert (
            client.post("/jobs/run", headers=auth, json={"run_id": "failed-run"}).json() == report
        )
        assert source.read.call_count == 1
        assert client.get("/jobs/status", headers=auth).json()["running"] is False


def test_demo_qualified_offer_end_to_end_is_local_and_deduplicated(
    settings, auth, request_data, now
):
    offer = request_data.snapshot.offer
    raw = offer.model_dump(mode="json", exclude={"source_name", "is_demo"})
    source = JSONOfferSource(
        json.dumps([raw, raw]).encode(), source_name=offer.source_name, is_demo=True
    )

    class Context:
        def prepare(self, incoming, as_of):
            return request_data.model_copy(
                update={
                    "as_of": as_of,
                    "snapshot": request_data.snapshot.model_copy(update={"offer": incoming}),
                }
            )

    settings = settings.model_copy(
        update={
            "deal": DealConfig(ai_review_required=True),
            "ai": AIConfig(enabled=True, allow_demo=True),
        }
    )
    app = create_app(settings, source=source, context=Context())
    with TestClient(app) as client:
        app.state.jobs.clock = lambda: now
        telegram_send = app.state.jobs.cycle.notifications.telegram.send = AsyncMock()
        first = client.post("/jobs/run", headers=auth, json={"run_id": "demo-1"}).json()
        assert first["processed"] == 1 and first["alerts"]["enqueued"] == 1
        assert first["alerts"]["delivery_sent"] == 1
        # A new cycle sees the same offer; its first immutable analysis is already NOTIFIED.
        second = client.post("/jobs/run", headers=auth, json={"run_id": "demo-2"}).json()
        assert second["processed"] == 1
        assert app.state.jobs.cycle.notifications.outbox.counts() == {"sent": 1}
        telegram_send.assert_not_called()


def test_http_conflict_when_other_process_holds_lock(settings, auth):
    with TestClient(create_app(settings)) as client:
        lease = JobStore(settings.jobs_db_path).acquire()
        assert lease is not None
        try:
            assert client.post("/jobs/run", headers=auth).status_code == 409
            assert client.get("/jobs/status", headers=auth).json()["running"] is True
        finally:
            lease.close()


def test_persistent_lock_excludes_another_process_and_recovers_after_exit(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "jobs.sqlite3"
    store = JobStore(path)
    lease = store.acquire()
    assert lease is not None
    script = (
        "from pathlib import Path; from app.scheduler.store import JobStore; "
        "import sys; lease = JobStore(Path(sys.argv[1])).acquire(); "
        'print("busy" if lease is None else "acquired"); '
        "lease.close() if lease else None"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "busy"
    lease.close()
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "acquired"


def test_interrupted_job_is_not_silently_run_twice(settings, now):
    from app.scheduler.store import CycleReport

    repository = SQLiteOfferRepository(settings.db_path)
    runtime = JobRuntime(settings, repository)
    runtime.manager.store.save(
        CycleReport(
            run_id="interrupted", status="running", started_at=now, source_name="demo", is_demo=True
        )
    )

    async def run():
        report = await runtime.manager.run("interrupted")
        assert report.status == "interrupted"
        assert report.processed == 0
        fresh = await runtime.manager.run("new-id")
        assert fresh.processed == 110
        await runtime.close()

    asyncio.run(run())
    repository.close()
