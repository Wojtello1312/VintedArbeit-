"""Synthetic public-page fixtures; all HTTP requests use MockTransport."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from vinted_deal_hunter import vinted_scraper
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.sources import SourceError
from vinted_deal_hunter.vinted import (
    ORIGIN,
    CollectionStopped,
    VintedOfferSource,
    VintedScrapeConfig,
    public_url,
    robots_allowed,
    search_url,
)

ITEM = f"{ORIGIN}/items/123-demo"


def html(item_id: str = "123", price: str = "12.30") -> str:
    return f'''<script type="application/ld+json">{{
        "@type":"Product", "name":"DEMO synthetic test shoe",
        "brand":"DEMO", "size":"EU 42", "offers":{{
            "@type":"Offer", "price":"{price}", "priceCurrency":"PLN",
            "url":"{ORIGIN}/items/{item_id}-demo",
            "itemCondition":"https://schema.org/UsedCondition"
        }}
    }}</script>'''


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def source_for(handler, urls=None, **config):
    clock = FakeTime()
    calls = []

    def transport(request):
        calls.append(request)
        return handler(request)

    source = VintedOfferSource(
        urls or [ITEM],
        VintedScrapeConfig(**config),
        transport=httpx.MockTransport(transport),
        sleep=clock.sleep,
        clock=clock.clock,
    )
    return source, calls, clock


def response(request, *, robots="User-agent: *\nAllow: /", content=None):
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text=robots, headers={"Content-Type": "text/plain"})
    return httpx.Response(
        200,
        text=content if content is not None else html(),
        headers={"Content-Type": "text/html", "Set-Cookie": "tracking=do-not-reuse"},
    )


def test_prices_utc_normalization_no_cookie_reuse_and_idempotence(settings, repository):
    source, calls, clock = source_for(response, urls=[ITEM, ITEM + "?tracking=yes"])
    first = import_offers(source, repository, settings)
    second = import_offers(source, repository, settings)
    assert (first.new, second.new, second.skipped) == (1, 0, 1)
    stored = repository.list_offers()[0]
    assert stored.asking_price == Decimal("12.30")
    assert stored.published_at is None
    assert stored.condition == "unknown"
    assert not stored.is_demo
    assert stored.source_name == "vinted-public-pl"
    page = source.report.pages[0]
    assert page.price_basis == "ASKING"
    assert page.observed_at.utcoffset().total_seconds() == 0
    assert source.report.finished_at >= source.report.started_at
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in calls)
    assert all(r.headers["user-agent"].startswith("VintedDealHunterTest/") for r in calls)
    assert all(r.extensions["timeout"]["read"] == 15 for r in calls)
    assert clock.sleeps == [5, 5, 5]


def test_catalog_discovery_deduplication_and_page_limit():
    def handler(request):
        if request.url.path == "/catalog":
            return response(
                request,
                content="""
                <a href="/items/123-demo">DEMO</a>
                <a href="/items/123-other-slug?x=1">duplicate</a>
                <a href="/items/456-demo">DEMO</a>
                <a href="https://evil.example/items/9-demo">external</a>
                <a href="/api/v2/items">private</a>
            """,
            )
        return response(request)

    source, calls, _ = source_for(handler, urls=[search_url("Nike 42")], max_pages=2)
    rows = source.read(max_records=10)
    assert len(rows) == 1 and source.report.truncated
    assert len(calls) == 3  # robots + catalog + one item
    assert [p.status for p in source.report.pages] == ["catalog", "collected"]


@pytest.mark.parametrize("status", [401, 403, 429, 301, 302, 307])
def test_access_denial_and_redirect_stop_without_retry(status):
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(request)
        return httpx.Response(status, headers={"Location": "http://127.0.0.1/private"})

    source, calls, _ = source_for(handler)
    with pytest.raises(CollectionStopped):
        source.read(max_records=10)
    assert len(calls) == 2
    assert source.report.stopped_reason


@pytest.mark.parametrize(
    "robots",
    [
        "User-agent: *\nDisallow: /items/",
        "User-agent: *\nDisallow: /*123*",
        "<html>challenge</html>",
    ],
)
def test_robots_prevents_item_fetch(robots):
    source, calls, _ = source_for(lambda r: response(r, robots=robots))
    with pytest.raises(CollectionStopped):
        source.read(max_records=10)
    assert len(calls) == 1


def test_robots_missing_fails_closed():
    source, calls, _ = source_for(lambda r: httpx.Response(404))
    with pytest.raises(CollectionStopped, match="robots_unavailable"):
        source.read(max_records=1)
    assert len(calls) == 1


def test_robots_specific_groups_wildcards_anchors_and_allow_tie():
    rules = """User-agent: *
Disallow: /
User-agent: VintedDealHunterTest
Disallow: /items/*
Allow: /items/123-demo$
User-agent: VintedDealHunterTest
Allow: /catalog
"""
    assert robots_allowed(rules, ITEM)
    assert not robots_allowed(rules, ITEM + "-extra")
    assert robots_allowed(rules, search_url("abc"))
    assert robots_allowed("User-agent: *\nDisallow: /items/\nAllow: /items/", ITEM)


def test_crawl_delay_and_retry_backoff():
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path == "/robots.txt":
            return response(request, robots="User-agent: *\nCrawl-delay: 9\nAllow: /")
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("secret must not be logged")
        return response(request)

    source, calls, clock = source_for(handler)
    assert len(source.read(max_records=10)) == 1
    assert len(calls) == 3
    assert clock.sleeps == [9, 11]


def test_5xx_retries_are_bounded_and_other_pages_continue():
    def handler(request):
        if request.url.path == "/items/123-demo":
            return httpx.Response(503)
        return response(request, content=html("456"))

    source, calls, _ = source_for(handler, urls=[ITEM, f"{ORIGIN}/items/456-demo"])
    assert len(source.read(max_records=10)) == 1
    assert len(calls) == 4
    assert source.report.pages[0].reason == "transient_failure_retries_exhausted"


def test_request_budget_counts_retries():
    def handler(request):
        return response(request) if request.url.path == "/robots.txt" else httpx.Response(503)

    source, calls, _ = source_for(handler, max_requests=2)
    with pytest.raises(CollectionStopped, match="request_limit"):
        source.read(max_records=10)
    assert len(calls) == 2


def test_retry_after_stops_without_early_retry():
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(request)
        return httpx.Response(503, headers={"Retry-After": "120"})

    source, calls, _ = source_for(handler)
    with pytest.raises(CollectionStopped, match="server_requested_retry_after"):
        source.read(max_records=10)
    assert len(calls) == 2


def test_request_rate_and_content_type():
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(request, robots="User-agent: *\nRequest-rate: 1/20\nAllow: /")
        return httpx.Response(200, json={"price": 25})

    source, _, clock = source_for(handler)
    assert source.read(max_records=10) == []
    assert clock.sleeps == [20]
    assert source.report.pages[0].reason == "unexpected_content_type"


def test_empty_search_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        vinted_scraper.main(["--search", " "])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "content",
    [
        html(price="12.345"),
        html("999"),
        "<html>No product metadata</html>",
        '<meta property="og:title" content="No currency or price">',
    ],
)
def test_bad_prices_missing_metadata_and_mismatched_products_are_reported(content):
    source, _, _ = source_for(lambda r: response(r, content=content))
    assert source.read(max_records=10) == []
    assert source.report.pages[0].status == "error"


def test_challenge_stops_and_no_partial_import(settings, repository):
    def handler(request):
        if request.url.path == "/items/456-demo":
            return response(request, content="<title>Just a moment...</title>")
        return response(request)

    source, calls, _ = source_for(handler, urls=[ITEM, f"{ORIGIN}/items/456-demo"])
    with pytest.raises(CollectionStopped, match="challenge"):
        import_offers(source, repository, settings)
    assert repository.list_offers() == []
    assert len(calls) == 3


def test_body_and_record_limits():
    source, _, _ = source_for(response, max_response_bytes=100)
    assert source.read(max_records=10) == []
    assert source.report.pages[0].reason == "response_size_limit"
    source, calls, _ = source_for(response, urls=[ITEM, f"{ORIGIN}/items/456-demo"])
    assert len(source.read(max_records=1)) == 1
    assert len(calls) == 2 and source.report.truncated


@pytest.mark.parametrize(
    "url",
    [
        "http://www.vinted.pl/items/123",
        "https://vinted.pl.evil.example/items/123",
        "https://www.vinted.pl/api/v2/items",
        "https://www.vinted.pl/member/123",
        "https://user:password@www.vinted.pl/items/123",
        "https://127.0.0.1/items/123",
        "https://www.vinted.pl:8443/items/123",
        "file:///tmp/items/123",
        "https://www.vinted.pl/items/123/../../api",
        "https://www.vinted.pl/items/123\n",
    ],
)
def test_invalid_urls_never_reach_transport(url):
    with pytest.raises(SourceError):
        public_url(url)


def test_cli_import_report_and_deduplication(tmp_path, capsys, monkeypatch):
    original = VintedOfferSource

    def offline(urls, config):
        return original(urls, config, transport=httpx.MockTransport(response), sleep=lambda _: None)

    monkeypatch.setattr(vinted_scraper, "VintedOfferSource", offline)
    db = tmp_path / "offers.sqlite3"
    args = ["--url", ITEM, "--engine", "http", "--db", str(db)]
    assert vinted_scraper.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["collection"]["pages"][0]["offer"]["asking_price"] == "12.30"
    assert first["import"]["new"] == 1
    assert vinted_scraper.main(args) == 0
    assert json.loads(capsys.readouterr().out)["import"]["skipped"] == 1


def test_cli_failure_report_does_not_claim_success(tmp_path, capsys, monkeypatch):
    original = VintedOfferSource

    def offline(urls, config):
        return original(urls, config, transport=httpx.MockTransport(lambda r: httpx.Response(403)))

    monkeypatch.setattr(vinted_scraper, "VintedOfferSource", offline)
    assert (
        vinted_scraper.main(["--url", ITEM, "--engine", "http", "--db", str(tmp_path / "offers")])
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["import"] is None
    assert result["collection"]["stopped_reason"] == "access_restricted_http_403"


def test_adapter_satisfies_existing_ingestion_contract(
    settings: Settings, repository: SQLiteOfferRepository
):
    source, _, _ = source_for(response)
    report = import_offers(source, repository, settings)
    assert (report.new, report.skipped, report.errors) == (1, 0, 0)


def test_cli_invalid_configuration_has_no_network(tmp_path: Path, capsys: pytest.CaptureFixture):
    assert (
        vinted_scraper.main(["--url", ITEM, "--interval", "0", "--db", str(tmp_path / "db")]) == 2
    )
    assert json.loads(capsys.readouterr().out)["collection"] is None
