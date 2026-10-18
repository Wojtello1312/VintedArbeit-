from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.admin.policy import PanelPolicy
from app.admin.queries import AdminQueries, OfferFilters
from app.admin.store import RevisionConflict
from tests.admin.helpers import csrf, login
from vinted_deal_hunter.api import create_app


def payload(client, **changes):
    data = {
        "csrf": csrf(client.get("/admin/settings")),
        "revision": "0",
        "watched_brands": "NIKE, Adidas, nike",
        "min_price": "10.01",
        "max_price": "400.99",
        "min_score": "72.5",
        "min_profit": "30.01",
        "interval_seconds": "600",
        "ai_daily_budget_usd": "0.123456789",
    }
    return {**data, **changes}


def test_settings_are_applied_and_persisted(signed_in, settings):
    response = signed_in.post(
        "/admin/settings", data=payload(signed_in, ai_enabled="on", scheduler_enabled="on")
    )
    assert response.status_code == 200 and "Ustawienia zapisane" in response.text
    rt = signed_in.app.state.runtime
    assert rt.settings.deal.allowed_brands == ("nike", "adidas")
    assert rt.settings.deal.approval_score == Decimal("72.5")
    assert rt.settings.deal.min_profit == Decimal("30.01")
    assert rt.settings.deal.min_price == Decimal("10.01")
    assert rt.settings.deal.max_price == Decimal("400.99")
    assert rt.settings.ai.enabled and rt.settings.deal.ai_review_required
    assert rt.settings.ai.daily_budget_usd == Decimal("0.123456789")
    assert rt.manager.scheduler.get_job("deal-cycle") is not None
    assert rt.manager.config.interval_seconds == 600
    assert rt.settings.telegram_bot_token == settings.telegram_bot_token
    with TestClient(create_app(settings)) as restarted:
        assert restarted.app.state.runtime.settings.deal.min_profit == Decimal("30.01")
        login(restarted)
        assert "72.5" in restarted.get("/admin/settings").text
    response = signed_in.post("/admin/settings", data=payload(signed_in, revision="1"))
    assert response.status_code == 200
    assert not rt.settings.ai.enabled and not rt.settings.deal.ai_review_required
    assert rt.manager.scheduler.get_job("deal-cycle") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"min_price": "500", "max_price": "100"},
        {"min_score": "101"},
        {"min_profit": "-1"},
        {"interval_seconds": "0"},
        {"ai_daily_budget_usd": "NaN"},
        {"db_path": "secret-path"},
        {"openai_api_key": "test-no-echo-secret"},
    ],
)
def test_invalid_settings_do_not_persist_or_echo_secrets(signed_in, changes):
    response = signed_in.post("/admin/settings", data=payload(signed_in, **changes))
    assert response.status_code == 422
    assert "test-no-echo-secret" not in response.text
    assert signed_in.app.state.runtime.panel.policy() == (0, None)


def test_stale_form_and_running_cycle(signed_in):
    form = payload(signed_in)
    assert signed_in.post("/admin/settings", data=form).status_code == 200
    assert signed_in.post("/admin/settings", data=form).status_code == 409
    rt = signed_in.app.state.runtime
    lease = rt.manager.store.acquire()
    assert lease is not None
    try:
        assert (
            signed_in.post("/admin/settings", data=payload(signed_in, revision="1")).status_code
            == 409
        )
        assert (
            signed_in.post(
                "/admin/jobs/run", data={"csrf": csrf(signed_in.get("/admin"))}
            ).status_code
            == 409
        )
    finally:
        lease.close()
    assert rt.panel.policy()[0] == 1


def test_job_controls_and_demo_idempotence(signed_in, settings):
    token = csrf(signed_in.get("/admin/system"))
    rt = signed_in.app.state.runtime
    for action in ("pause", "resume"):
        response = signed_in.post(f"/admin/jobs/{action}", data={"csrf": token})
        assert response.status_code == 200
        assert rt.manager.store.paused() == (action == "pause")
    assert signed_in.post("/admin/jobs/run", data={"csrf": token}).status_code == 200
    stats = AdminQueries(settings).dashboard()
    assert stats["total"] > 0 and stats["demo_count"] == stats["total"]
    assert signed_in.post("/admin/jobs/run", data={"csrf": token}).status_code == 200
    assert AdminQueries(settings).dashboard() == stats
    assert AdminQueries(settings).offers(OfferFilters(mode="production"))[1] == 0
    assert len(AdminQueries(settings).system()["jobs"]) == 2
    assert "Zakończony" in signed_in.get("/admin/system").text


def test_store_policy_conflict_and_login_window(client, settings):
    store = client.app.state.runtime.panel
    now = datetime.now(UTC)
    policy = PanelPolicy.from_settings(settings)
    assert store.save_policy(policy, 0, now) == 1
    with pytest.raises(RevisionConflict):
        store.save_policy(policy, 0, now)
    assert store.allow_login("test", now, 1, 60)
    assert not store.allow_login("test", now + timedelta(seconds=59), 1, 60)
    assert store.allow_login("test", now + timedelta(seconds=61), 1, 60)
