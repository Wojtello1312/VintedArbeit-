"""Synthetic fixtures exercise the real UI/SQLite pipeline without external traffic."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.admin.queries import AdminQueries, OfferFilters, offer_key
from app.notifications.formatting import AlertMessage
from app.scheduler.store import JobStore
from tests.admin.helpers import csrf, login
from tests.admin.test_settings import payload
from tests.test_vinted_playwright import FakeBrowser, document, item_html, robots_transport
from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.db import Disposition
from vinted_deal_hunter.models import OfferInput
from vinted_deal_hunter.sources import SourceError, SourceErrorCode
from vinted_deal_hunter.vinted import ORIGIN
from vinted_deal_hunter.vinted_playwright import VintedPlaywrightOfferSource


def offer(external="123", **changes):
    # Synthetic public-source identities, never fetched from Vinted.
    return OfferInput.model_validate(
        {
            "source_name": "vinted-public-pl",
            "external_id": external,
            "is_demo": False,
            "product_name": "SYNTHETIC shoe",
            "currency": "PLN",
            "asking_price": "99.99",
            "condition": "very_good",
            "category": "Buty",
            **changes,
        }
    )


def collection_form(client, **changes):
    return {
        "csrf": csrf(client.get("/admin/system")),
        "revision": "0",
        "enabled": "on",
        "search": "Nike Air Max 90",
        "catalog_url": "",
        "max_pages": "10",
        "browser_channel": "msedge",
        "currency": "PLN",
        "max_price": "100",
        "min_price": "",
        "condition": "very_good",
        "category": "Buty",
        "brand": "",
        "size": "",
        **changes,
    }


def test_list_defaults_to_real_all_currencies_with_canonical_links(signed_in, seeded, settings):
    repo = signed_in.app.state.repository
    repo.add_many([offer(), offer("456", currency="DKK"), offer("789", currency="SEK")])
    response = signed_in.get("/admin/offers")
    assert response.status_code == 200
    assert seeded.request.snapshot.offer.product_name not in response.text
    for identifier in ("123", "456", "789"):
        assert f'href="{ORIGIN}/items/{identifier}"' in response.text
    assert "DKK" in response.text and "SEK" in response.text
    key = offer_key("vinted-public-pl", "123")
    detail = signed_in.get(f"/admin/offers/{key}")
    assert f'href="{ORIGIN}/items/123"' in detail.text
    assert "Bardzo dobry" in detail.text and "Buty" in detail.text
    assert AdminQueries(settings).dashboard(production=True)["total"] == 3
    assert (
        seeded.request.snapshot.offer.product_name in signed_in.get("/admin/offers?mode=demo").text
    )


def test_deletion_is_authenticated_csrf_protected_and_persistent(signed_in, settings):
    repo = signed_in.app.state.repository
    item = offer()
    repo.add_many([item])
    path = f"/admin/offers/{offer_key(item.source_name, item.external_id)}/delete"
    assert signed_in.post(path, data={"csrf": "wrong"}).status_code == 403
    token = csrf(signed_in.get("/admin/offers"))
    assert signed_in.post(path, data={"csrf": token}).status_code == 200
    assert repo.list_offers() == []
    assert repo.add_many([item]) == [Disposition.DELETED]
    with TestClient(create_app(settings)) as restarted:
        login(restarted)
        assert restarted.app.state.repository.add_many([item]) == [Disposition.DELETED]
        assert restarted.get(path.removesuffix("/delete")).status_code == 404
    signed_in.cookies.clear()
    assert signed_in.post(path, data={"csrf": token}, follow_redirects=False).status_code == 303


def test_demo_cleanup_preserves_real_offers_and_analysis_history(signed_in, seeded, settings):
    repo = signed_in.app.state.repository
    repo.add_many([offer()])
    token = csrf(signed_in.get("/admin/offers"))
    assert signed_in.post("/admin/offers/demo/delete", data={"csrf": token}).status_code == 200
    assert repo.list_offers() == [offer()]
    assert repo.add_many([seeded.request.snapshot.offer]) == [Disposition.DELETED]
    assert AdminQueries(settings).history(seeded.request.snapshot.offer)[1] == 1


def test_deletion_cancels_pending_notifications(signed_in, seeded):
    notifications = signed_in.app.state.runtime.manager.cycle.notifications
    item = seeded.request.snapshot.offer
    message = AlertMessage(
        alert_key="synthetic-pending-alert",
        analysis_id=seeded.analysis_id,
        source_name=item.source_name,
        external_id=item.external_id,
        is_demo=True,
        destination="demo",
        html="SYNTHETIC test notification",
    )
    assert notifications.outbox.enqueue(message, datetime.now(UTC))
    assert notifications.outbox.counts() == {"pending": 1}
    key = offer_key(item.source_name, item.external_id)
    token = csrf(signed_in.get("/admin/offers"))
    assert signed_in.post(f"/admin/offers/{key}/delete", data={"csrf": token}).status_code == 200
    assert notifications.outbox.counts() == {"cancelled": 1}


def test_price_sort_and_condition_filters(signed_in, settings):
    repo = signed_in.app.state.repository
    repo.add_many(
        [
            offer("123", asking_price="100.01"),
            offer("456", asking_price="9.99"),
            offer("789", asking_price="100.00", condition="unknown"),
        ]
    )
    queries = AdminQueries(settings)
    rows, total = queries.offers(OfferFilters(sort="price_asc", max_price=Decimal("100")))
    assert total == 2 and [row.offer.external_id for row in rows] == ["456", "789"]
    rows, total = queries.offers(OfferFilters(condition="very_good", category="Buty"))
    assert total == 2
    assert [row.offer.external_id for row in queries.offers(OfferFilters())[0]] == [
        "789",
        "456",
        "123",
    ]
    assert signed_in.get("/admin/offers?currency=all&max_price=100").status_code == 422


def test_collection_policy_changes_source_and_survives_settings_and_restart(signed_in, settings):
    form = collection_form(signed_in)
    assert signed_in.post("/admin/collection", data=form).status_code == 200
    rt = signed_in.app.state.runtime
    source = rt.manager.cycle.source
    assert isinstance(source, VintedPlaywrightOfferSource)
    assert source.filters.max_price == Decimal("100")
    assert source.filters.condition == "very_good" and source.filters.category == "Buty"
    assert "price_to=100" in source.urls[0] and "status_ids%5B%5D=2" in source.urls[0]
    assert signed_in.post("/admin/collection", data=form).status_code == 409
    assert (
        signed_in.post("/admin/settings", data=payload(signed_in, revision="1")).status_code == 200
    )
    with TestClient(create_app(settings)) as restarted:
        persisted = restarted.app.state.runtime.manager.cycle.source
        assert isinstance(persisted, VintedPlaywrightOfferSource)
        assert persisted.filters == source.filters
        assert persisted.urls == source.urls


@pytest.mark.parametrize(
    "changes",
    [
        {"max_price": "NaN"},
        {"min_price": "101"},
        {"catalog_url": "https://evil.example/catalog", "search": ""},
        {"catalog_url": ORIGIN + "/api/v2/items", "search": ""},
        {"condition": "perfect"},
        {"browser_executable": "/secret"},
        {"max_pages": "101"},
    ],
)
def test_invalid_collection_settings_are_rejected(signed_in, changes):
    assert (
        signed_in.post("/admin/collection", data=collection_form(signed_in, **changes)).status_code
        == 422
    )
    assert signed_in.app.state.runtime.panel.policy() == (0, None)


def test_collection_and_progress_require_session_and_csrf(client, signed_in):
    form = collection_form(signed_in, csrf="wrong")
    assert signed_in.post("/admin/collection", data=form).status_code == 403
    signed_in.cookies.clear()
    assert client.get("/admin/jobs/progress", follow_redirects=False).status_code == 303
    assert client.post("/admin/collection", data=form, follow_redirects=False).status_code == 303


async def wait_for_task(manager):
    return await manager._task


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (SourceErrorCode.CHROMIUM_NOT_INSTALLED, "Brak przeglądarki Chromium"),
        (SourceErrorCode.BROWSER_NOT_INSTALLED, "Nie znaleziono wybranej przeglądarki"),
        (SourceErrorCode.PLAYWRIGHT_NOT_INSTALLED, "uv sync --locked --extra browser"),
        (SourceErrorCode.BROWSER_START_FAILED, "Nie udało się uruchomić przeglądarki"),
        (SourceErrorCode.BROWSER_SUBPROCESS_UNSUPPORTED, "Proactor"),
        (None, "Ten cykl nie zawiera szczegółowego kodu przyczyny"),
    ],
)
def test_source_failure_diagnosis_in_progress_api_history_and_logs(
    signed_in, settings, caplog, code, hint
):
    assert signed_in.post("/admin/collection", data=collection_form(signed_in)).status_code == 200
    manager = signed_in.app.state.runtime.manager
    source = manager.cycle.source
    source.robots_transport = robots_transport()
    source.session_factory = Mock(side_effect=SourceError("private driver credentials", code=code))
    token = csrf(signed_in.get("/admin/system"))
    response = signed_in.post(
        "/admin/jobs/run", data={"csrf": token}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 202
    report = signed_in.portal.call(wait_for_task, manager)
    assert report.status == "error" and report.error_code == code
    assert JobStore(settings.jobs_db_path).latest().error_code == code
    progress = signed_in.get("/admin/jobs/progress")
    system = signed_in.get("/admin/system")
    assert hint in progress.text and hint in system.text
    headers = {"Authorization": "Bearer " + settings.jobs_api_token.get_secret_value()}
    status = signed_in.get("/jobs/status", headers=headers)
    assert status.json()["last_run"]["error_code"] == code
    assert not status.json()["running"]
    assert "code=" + (code or "unknown") in caplog.text
    assert (
        "private driver credentials" not in progress.text + system.text + status.text + caplog.text
    )
    source.session_factory.assert_called_once()


def test_background_cycle_progress_lock_filters_import_and_dashboard(
    signed_in, monkeypatch, settings
):
    assert signed_in.post("/admin/collection", data=collection_form(signed_in)).status_code == 200
    rt = signed_in.app.state.runtime
    source = rt.manager.cycle.source
    catalog = source.urls[0]
    item1, item2 = ORIGIN + "/items/123-demo", ORIGIN + "/items/456-demo"
    attrs = (
        b'<div data-testid="item-attributes-status"><span>Stan</span>'
        b"<span>Bardzo dobry</span></div>"
        b'<div data-testid="item-attributes-category">Buty</div>'
    )
    browser = FakeBrowser(
        {
            catalog: [document(catalog, links=(item1, item2))],
            item1: [document(item1, html=item_html("123", "100.00") + attrs)],
            item2: [document(item2, html=item_html("456", "100.01") + attrs)],
        }
    )
    entered, release = Event(), Event()

    def session_factory(_):
        entered.set()
        assert release.wait(timeout=10)
        return browser

    source.robots_transport = robots_transport()
    source.session_factory = session_factory
    source.sleep = lambda _: None
    token = csrf(signed_in.get("/admin/system"))
    try:
        response = signed_in.post(
            "/admin/jobs/run", data={"csrf": token}, headers={"HX-Request": "true"}
        )
        assert response.status_code == 202
        assert entered.wait(timeout=5)
        progress = signed_in.get("/admin/jobs/progress")
        assert "Cykl w toku" in progress.text and "cycle-spinner" in progress.text
        assert "every 2s" in progress.text
        assert signed_in.post("/admin/jobs/run", data={"csrf": token}).status_code == 409
        assert (
            signed_in.post(
                "/admin/collection", data=collection_form(signed_in, revision="1")
            ).status_code
            == 409
        )
        assert signed_in.post("/admin/offers/demo/delete", data={"csrf": token}).status_code == 409
    finally:
        release.set()
    report = signed_in.portal.call(wait_for_task, rt.manager)
    assert report.status == report.phase == "completed"
    assert (report.pages, report.fetched, report.imported, report.excluded, report.processed) == (
        3,
        1,
        1,
        1,
        1,
    )
    assert report.exclusion_reasons == {"price_above_maximum": 1}
    page = signed_in.get("/admin/offers")
    assert f'href="{ORIGIN}/items/123"' in page.text
    assert f'href="{ORIGIN}/items/456"' not in page.text
    assert "Zakończony" in signed_in.get("/admin/jobs/progress").text
    assert AdminQueries(settings).dashboard(production=True)["total"] == 1


def test_old_payload_without_category_remains_a_duplicate(signed_in):
    from sqlalchemy import text

    repo = signed_in.app.state.repository
    item = offer(category=None)
    repo.add_many([item])
    old = item.model_dump(mode="json", exclude={"category"})
    with repo.engine.begin() as db:
        db.execute(text("UPDATE offers SET payload=:payload"), {"payload": json.dumps(old)})
    assert repo.add_many([item]) == [Disposition.DUPLICATE]
