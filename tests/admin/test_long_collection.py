"""Long cycles with synthetic pages only: durable progress, interruption and restart."""

from threading import Event

import pytest
from fastapi.testclient import TestClient

from tests.admin.helpers import csrf, login
from tests.admin.test_collection import collection_form, wait_for_task
from tests.test_vinted_playwright import FakeBrowser, document, item_html, robots_transport
from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.vinted import ORIGIN


def prepare_source(client):
    form = collection_form(client, max_pages="100", condition="", category="", max_price="")
    assert client.post("/admin/collection", data=form).status_code == 200
    source = client.app.state.runtime.manager.cycle.source
    source.robots_transport = robots_transport()
    source.sleep = lambda _: None
    return source


def browser_pages(source, count=110):
    urls = [f"{ORIGIN}/items/{i}-synthetic" for i in range(1001, 1001 + count)]
    documents = {source.urls[0]: [document(source.urls[0], links=tuple(urls))]}
    documents.update(
        {url: [document(url, html=item_html(str(i)))] for i, url in enumerate(urls, start=1001)}
    )
    return urls, documents


def auth(settings):
    return {"Authorization": "Bearer " + settings.jobs_api_token.get_secret_value()}


def test_100_page_cycle_commits_and_exposes_offers_while_still_running(signed_in, settings):
    source = prepare_source(signed_in)
    urls, documents = browser_pages(source)
    browser = FakeBrowser(documents)
    original = browser.navigate
    entered, release = Event(), Event()

    def navigate(url, *, catalog):
        if url == urls[60]:
            entered.set()
            assert release.wait(timeout=10)
        return original(url, catalog=catalog)

    browser.navigate = navigate
    source.session_factory = lambda _: browser
    token = csrf(signed_in.get("/admin/system"))
    try:
        assert (
            signed_in.post(
                "/admin/jobs/run", data={"csrf": token}, headers={"HX-Request": "true"}
            ).status_code
            == 202
        )
        assert entered.wait(timeout=10)
        status = signed_in.get("/jobs/status", headers=auth(settings)).json()
        assert status["running"]
        assert status["last_run"]["imported"] == 60
        assert len(signed_in.app.state.repository.list_offers()) == 60
        assert f'href="{ORIGIN}/items/1060"' in signed_in.get("/admin/offers").text
    finally:
        release.set()
    result = signed_in.portal.call(wait_for_task, signed_in.app.state.runtime.manager)
    assert result.status == "completed" and result.error_type is None
    assert result.pages == result.max_pages == 100
    assert result.imported == result.processed == 99  # One catalog plus 99 item pages.
    assert result.requests == 101 and result.collection_end_reason == "page_limit"
    assert len(browser.calls) == 100 and browser.closed


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("403", "access_restricted_http_403"),
        ("429", "access_restricted_http_429"),
        ("challenge", "challenge_requires_manual_review"),
        ("budget", "request_limit"),
        ("browser_crash", None),
    ],
)
def test_stop_after_60_preserves_and_analyzes_offers_then_deduplicates_on_restart(
    signed_in, settings, failure, expected_code
):
    source = prepare_source(signed_in)
    urls, documents = browser_pages(source)
    if failure == "budget":
        source.config = source.config.model_copy(update={"max_requests": 62})
    elif failure == "browser_crash":
        documents[urls[60]] = [RuntimeError("private browser credentials")]
    elif failure == "challenge":
        documents[urls[60]] = [document(urls[60], title="Just a moment...")]
    else:
        documents[urls[60]] = [document(urls[60], status=int(failure))]
    browser = FakeBrowser(documents)
    source.session_factory = lambda _: browser
    response = signed_in.post("/jobs/run", headers=auth(settings), json={"run_id": "long-failed"})
    result = response.json()
    assert result["status"] == result["phase"] == "partial"
    assert result["collection_incomplete"] and result["error_code"] == expected_code
    assert result["imported"] == result["fetched"] == result["processed"] == 60
    assert result["pages"] == 61
    assert result["requests"] == (62 if failure == "budget" else 63)
    assert len(browser.calls) == (61 if failure == "budget" else 62)
    assert browser.closed
    assert len(signed_in.app.state.repository.list_offers()) == 60
    progress = signed_in.get("/admin/jobs/progress")
    assert "Zakończony częściowo" in progress.text
    assert "Zachowano wcześniej zapisane oferty" in progress.text
    assert "private browser credentials" not in response.text + progress.text

    # Reopen all stores, then explicitly run a new cycle with a now-available synthetic source.
    with TestClient(create_app(settings)) as restarted:
        login(restarted)
        cached = restarted.post("/jobs/run", headers=auth(settings), json={"run_id": "long-failed"})
        assert cached.json() == result  # Same ID does not issue any new network requests.
        assert f'href="{ORIGIN}/items/1060"' in restarted.get("/admin/offers").text
        resumed_source = restarted.app.state.runtime.manager.cycle.source
        _, resumed_documents = browser_pages(resumed_source)
        resumed_browser = FakeBrowser(resumed_documents)
        resumed_source.robots_transport = robots_transport()
        resumed_source.sleep = lambda _: None
        resumed_source.session_factory = lambda _: resumed_browser
        resumed = restarted.post(
            "/jobs/run", headers=auth(settings), json={"run_id": "long-resumed"}
        ).json()
        assert resumed["status"] == "completed"
        assert (resumed["imported"], resumed["skipped"], resumed["processed"]) == (39, 60, 99)
        assert len(restarted.app.state.repository.list_offers()) == 99
        assert resumed["error_code"] is None and resumed["error_type"] is None


def test_short_catalog_is_reported_as_exhausted_not_failed(signed_in, settings):
    source = prepare_source(signed_in)
    _, documents = browser_pages(source, count=60)
    source.session_factory = lambda _: FakeBrowser(documents)
    report = signed_in.post("/jobs/run", headers=auth(settings), json={"run_id": "short"}).json()
    assert report["status"] == "completed" and report["pages"] == 61
    assert report["imported"] == 60 and report["collection_end_reason"] == "queue_exhausted"
    assert "Sprawdzono wszystkie dostępne linki" in signed_in.get("/admin/jobs/progress").text
