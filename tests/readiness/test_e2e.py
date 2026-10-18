import asyncio
import re
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from app.ai.budget import AIBudget
from app.ai.client import ClientResult
from app.ai.reviewer import AIReviewer, ReviewedDealPipeline
from app.ai.schemas import AIReview, TokenUsage
from app.core.deal_pipeline import DealPipeline
from app.demo import DEMO_TIME, SOURCE, demo_content, demo_request
from app.notifications.telegram import DemoNotifier
from app.schemas.deals import DealState
from vinted_deal_hunter.api import create_app


def test_100_offers_full_pipeline_persistence_restart_and_no_duplicate_calls(settings, auth):
    ai_config = settings.ai.model_copy(
        update={
            "enabled": True,
            "allow_demo": True,
            "max_daily_analyses": 100,
            "input_usd_per_million": Decimal("1"),
            "output_usd_per_million": Decimal("2"),
            "daily_budget_usd": Decimal("10"),
        }
    )
    review = AIReview(
        listing_consistency=98,
        risk_level="low",
        detected_issues=[],
        product_match_valid=True,
        manual_review_required=False,
        explanation="DEMO mocked reviewer; no authenticity guarantee.",
        confidence=0.95,
    )
    mocked_api = Mock()
    mocked_api.review.return_value = ClientResult(
        review=review,
        usage=TokenUsage(input_tokens=500, output_tokens=100, cached_input_tokens=0),
        model=ai_config.model,
    )
    mocked_telegram = DemoNotifier()
    mocked_telegram.send = AsyncMock(wraps=mocked_telegram.send)
    with TestClient(create_app(settings), headers=auth) as client:
        assert client.get("/health/ready").status_code == 200
        for expected_new in (100, 0):
            response = client.post(
                f"/imports?source_name={SOURCE}",
                content=demo_content(),
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 200
            assert response.json()["new"] == expected_new
            assert response.json()["skipped"] == 100 - expected_new
            assert response.json()["errors"] == 0
        rt = client.app.state.runtime
        offers = client.app.state.repository.list_offers()
        assert len(offers) == 100
        assert all(
            o.is_demo
            and o.product_name == "DEMO Runner"
            and o.brand == "demo brand"
            and o.currency == "PLN"
            and o.size == "EU 42"
            for o in offers
        )
        pipeline = DealPipeline(
            rt.analysis_store, settings.deal.model_copy(update={"ai_review_required": True})
        )
        reviewer = AIReviewer(
            ai_config, api_key="test-only-injected-mock", client=mocked_api, clock=lambda: DEMO_TIME
        )
        reviewed = ReviewedDealPipeline(pipeline, reviewer)
        notifier = rt.manager.cycle.notifications
        notifier.pipeline = pipeline
        notifier.demo = mocked_telegram
        records = reviewed.process_batch(demo_request(offer) for offer in offers)
        assert not records.failures and len(records.records) == 100
        approved = [r for r in records.records if r.state == DealState.APPROVED]
        assert len(approved) == 80
        assert sum(r.state == DealState.REJECTED for r in records.records) == 10
        assert sum(r.state == DealState.REVIEW_REQUIRED for r in records.records) == 10
        for record in approved:
            assert record.estimate.sample_size == 20 and record.estimate.estimated_price is not None
            assert record.ai_review.status == "completed"
            assert record.score.total >= 65
            assert {h.current for h in record.history} >= {
                DealState.NORMALIZED,
                DealState.FILTERED,
                DealState.MATCHED,
                DealState.VALUED,
                DealState.SCORED,
                DealState.AI_PENDING,
                DealState.APPROVED,
            }
            assert notifier.enqueue(record) == "enqueued"
            assert notifier.enqueue(record) == "duplicate"
        asyncio.run(notifier.flush())
        asyncio.run(notifier.flush())  # default batch size 50; remaining 30 in second flush
        assert mocked_telegram.send.await_count == mocked_api.review.call_count == 80
        assert AIBudget(ai_config).daily_usage(DEMO_TIME).accounted_usd == Decimal("0.056")
        final = reviewed.process_batch(demo_request(offer) for offer in offers)
        assert sum(r.state == DealState.NOTIFIED for r in final.records) == 80
        assert mocked_api.review.call_count == 80
        asyncio.run(notifier.flush())
        assert mocked_telegram.send.await_count == 80
        page = client.get("/offers?min_score=65&sort=score").json()
        assert page["total"] == 80 and len(page["items"]) == 25
        detail = client.get("/offers/" + page["items"][0]["key"]).json()
        assert detail["analysis"]["state"] == "NOTIFIED"
        login = client.get("/admin/login")
        token = re.search('name="csrf" value="([^"]+)"', login.text).group(1)
        assert (
            client.post(
                "/admin/login",
                data={"csrf": token, "username": "admin", "password": "test-readiness-password"},
            ).status_code
            == 200
        )
        dashboard = client.get("/admin")
        assert "100 ofert demo" in dashboard.text and "DEMO Runner" in dashboard.text
        assert "80" in client.get("/admin/deals").text
        assert len(rt.manager.cycle.notifications.outbox.unreconciled()) == 0
    with TestClient(create_app(settings), headers=auth) as restarted:
        assert restarted.get("/health/ready").status_code == 200
        assert restarted.get("/offers").json()["total"] == 100
        assert restarted.get("/offers?min_score=65").json()["total"] == 80
        assert (
            restarted.post(
                f"/imports?source_name={SOURCE}",
                content=demo_content(),
                headers={"Content-Type": "application/json"},
            ).json()["new"]
            == 0
        )
        rt = restarted.app.state.runtime
        processed = ReviewedDealPipeline(
            DealPipeline(
                rt.analysis_store, settings.deal.model_copy(update={"ai_review_required": True})
            ),
            AIReviewer(
                ai_config,
                api_key="test-only-injected-mock",
                client=mocked_api,
                clock=lambda: DEMO_TIME,
            ),
        )
        again = processed.process_batch(
            demo_request(o) for o in restarted.app.state.repository.list_offers()
        )
        assert not again.failures
        assert sum(r.state == DealState.NOTIFIED for r in again.records) == 80
        assert mocked_api.review.call_count == 80


def test_demo_command_is_offline_repeatable_and_completes_all_local_deliveries(
    tmp_path, monkeypatch, capsys
):
    import json

    from app.demo import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDH_DB_PATH", str(tmp_path / "offers.sqlite3"))
    monkeypatch.setenv("VDH_AI", '{"enabled":true}')
    monkeypatch.setenv("VDH_DEAL", '{"ai_review_required":true}')
    monkeypatch.setenv("OPENAI_API_KEY", "must-never-be-used-by-demo")
    main()
    first = json.loads(capsys.readouterr().out)
    assert first["import"]["new"] == 100
    assert first["local_deliveries"]["sent"] == 80
    main()
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["import"]["new"] == 0
    assert repeated["local_deliveries"].get("sent", 0) == 0
    assert repeated["states_before_delivery"]["NOTIFIED"] == 80
