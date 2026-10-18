from datetime import UTC, datetime
from importlib.resources import files
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.backtest import HistoricalDemo
from app.core.deal_pipeline import DealPipeline
from app.notifications.config import NotificationConfig
from app.notifications.formatting import destination_key
from app.notifications.outbox import AlertOutbox
from app.notifications.service import NotificationService
from app.notifications.telegram import DeliveryResult
from app.schemas.deals import AnalysisRequest, DealState
from app.storage.memory import MemoryAnalysisStore
from vinted_deal_hunter.config import Settings


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network is forbidden in alerting/scheduler tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "CHAT_ID",
        "JOBS_API_TOKEN",
        "VDH_NOTIFICATIONS",
        "VDH_SCHEDULER",
        "VDH_AI",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def now():
    return datetime(2026, 1, 10, 12, tzinfo=UTC)


@pytest.fixture
def moment(now):
    return [now]


@pytest.fixture
def request_data():
    demo = HistoricalDemo.model_validate_json(
        files("app").joinpath("data/backtest_demo.json").read_text()
    )
    snapshot = demo.snapshots[0].model_copy(
        update={
            "listing_url": "https://example.com/DEMO/item?x=1&y=2",
        }
    )
    return AnalysisRequest(
        snapshot=snapshot, observations=demo.observations, as_of=snapshot.available_at
    )


@pytest.fixture
def pipeline(moment):
    return DealPipeline(MemoryAnalysisStore(), clock=lambda: moment[0])


@pytest.fixture
def approved(pipeline, request_data):
    record = pipeline.process(request_data)
    assert record.state == DealState.APPROVED
    return record


@pytest.fixture
def live_request(request_data):
    # Synthetic transport fixture explicitly confined to tests; never a production dataset.
    snapshot = request_data.snapshot.model_copy(
        update={"offer": request_data.snapshot.offer.model_copy(update={"is_demo": False})}
    )
    observations = tuple(
        item.model_copy(
            update={"observation": item.observation.model_copy(update={"is_demo": False})}
        )
        for item in request_data.observations
    )
    return request_data.model_copy(update={"snapshot": snapshot, "observations": observations})


@pytest.fixture
def live_approved(pipeline, live_request):
    record = pipeline.process(live_request)
    assert record.state == DealState.APPROVED
    return record


@pytest.fixture
def notification_config():
    return NotificationConfig(enabled=True, daily_limit=2)


@pytest.fixture
def outbox(tmp_path, notification_config):
    return AlertOutbox(tmp_path / "jobs.sqlite3", notification_config)


@pytest.fixture
def notifier():
    class FakeNotifier:
        ready = True
        send = AsyncMock(return_value=DeliveryResult(status="sent", reason="TEST", receipt_id="1"))

    return FakeNotifier()


@pytest.fixture
def service(outbox, pipeline, notifier, moment):
    return NotificationService(
        outbox, pipeline, notifier, destination_key("123"), clock=lambda: moment[0]
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "offers.sqlite3", jobs_api_token=SecretStr("test-job-token-" + "a" * 32)
    )


@pytest.fixture
def auth(settings):
    return {"Authorization": "Bearer " + settings.jobs_api_token.get_secret_value()}
