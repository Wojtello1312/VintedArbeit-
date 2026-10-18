from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from app.ai.client import ClientResult
from app.ai.config import AIConfig
from app.ai.schemas import AIReview, TokenUsage
from app.backtest import HistoricalDemo
from app.core.deal_pipeline import DealPipeline
from app.deal_config import DealConfig
from app.schemas.deals import AnalysisRequest, DealState
from app.storage.memory import MemoryAnalysisStore


@pytest.fixture(autouse=True)
def no_paid_api(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in AI tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "VDH_AI"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def now():
    return datetime(2026, 1, 10, 12, tzinfo=UTC)


@pytest.fixture
def ai_config(tmp_path: Path):
    # Fictional TEST tariff, not a production price claim.
    return AIConfig(
        enabled=True,
        allow_demo=True,
        db_path=tmp_path / "ai.sqlite3",
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("2"),
        cached_input_usd_per_million=Decimal("0.5"),
    )


@pytest.fixture
def ai_request():
    dataset = HistoricalDemo.model_validate_json(
        files("app").joinpath("data/backtest_demo.json").read_text()
    )
    snapshot = dataset.snapshots[0]
    return AnalysisRequest(
        snapshot=snapshot,
        observations=dataset.observations,
        as_of=snapshot.available_at,
    )


@pytest.fixture
def pipeline(now):
    return DealPipeline(
        MemoryAnalysisStore(), DealConfig(ai_review_required=True), clock=lambda: now
    )


@pytest.fixture
def pending(pipeline, ai_request):
    record = pipeline.process(ai_request)
    assert record.state == DealState.AI_PENDING
    return record


@pytest.fixture
def review_data():
    return {
        "listing_consistency": 92,
        "risk_level": "low",
        "detected_issues": [],
        "product_match_valid": True,
        "manual_review_required": False,
        "explanation": "DEMO: metadata is consistent; authenticity cannot be established.",
        "confidence": 0.8,
    }


@pytest.fixture
def api_result(review_data):
    return ClientResult(
        AIReview.model_validate(review_data),
        TokenUsage(input_tokens=1000, output_tokens=200, cached_input_tokens=100),
        "gpt-5-nano-test-snapshot",
    )


@pytest.fixture
def fake_client(api_result):
    return Mock(review=Mock(return_value=api_result))
