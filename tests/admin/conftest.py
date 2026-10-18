from importlib.resources import files

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.admin.password import hash_password
from app.ai.config import AIConfig
from app.backtest import HistoricalDemo
from app.core.deal_pipeline import DealPipeline
from app.scheduler.config import SchedulerConfig
from app.schemas.deals import AnalysisRequest, DealState
from tests.admin.helpers import PASSWORD, login
from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Admin tests must not contact external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(scope="session")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def settings(tmp_path, password_hash):
    return Settings(
        db_path=tmp_path / "offers.sqlite3",
        ai=AIConfig(db_path=tmp_path / "ai.sqlite3"),
        scheduler=SchedulerConfig(enabled=False),
        admin_password_hash=SecretStr(password_hash),
        jobs_api_token=SecretStr("test-jobs-secret-" + "x" * 32),
        telegram_bot_token=SecretStr("123456:test_telegram_secret_" + "x" * 32),
        chat_id=SecretStr("-123456789"),
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def signed_in(client):
    login(client)
    return client


@pytest.fixture
def seeded(client, settings):
    demo = HistoricalDemo.model_validate_json(
        files("app").joinpath("data/backtest_demo.json").read_text()
    )
    snapshot = demo.snapshots[0]
    request = AnalysisRequest(
        snapshot=snapshot,
        observations=demo.observations,
        as_of=snapshot.available_at,
    )
    repository = SQLiteOfferRepository(settings.db_path)
    repository.add_many([snapshot.offer])
    repository.close()
    record = DealPipeline(client.app.state.runtime.analysis_store).process(request)
    assert record.state == DealState.APPROVED
    return record
