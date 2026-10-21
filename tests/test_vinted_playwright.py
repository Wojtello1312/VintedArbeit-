"""Playwright orchestration tests use a fake browser and synthetic HTML."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import Mock

import httpx
import portalocker
import pytest

from app.scheduler.runtime import JobRuntime
from vinted_deal_hunter import vinted_scraper
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.sources import SourceError, SourceErrorCode
from vinted_deal_hunter.vinted import ORIGIN, CollectionStopped, VintedOfferSource, search_url
from vinted_deal_hunter.vinted_playwright import (
    BrowserDocument,
    BrowserNavigationError,
    VintedAutomationConfig,
    VintedPlaywrightConfig,
    VintedPlaywrightOfferSource,
    _PlaywrightBrowserSession,
    configured_vinted_source,
)

ITEM_123 = f"{ORIGIN}/items/123-demo"
ITEM_456 = f"{ORIGIN}/items/456-demo"


def test_missing_playwright_has_safe_setup_diagnosis(monkeypatch):
    monkeypatch.setattr(
        "vinted_deal_hunter.vinted_playwright.importlib.import_module",
        Mock(side_effect=ImportError("private installation path")),
    )
    with pytest.raises(SourceError) as caught:
        _PlaywrightBrowserSession(VintedPlaywrightConfig())
    assert caught.value.code == SourceErrorCode.PLAYWRIGHT_NOT_INSTALLED
    assert "uv sync --locked --extra browser" in str(caught.value)
    assert "private installation path" not in str(caught.value)


@pytest.mark.parametrize(
    ("channel", "failure", "expected"),
    [
        (
            "chromium",
            RuntimeError("Executable doesn't exist at /private/browser"),
            SourceErrorCode.CHROMIUM_NOT_INSTALLED,
        ),
        (
            "msedge",
            RuntimeError("Chromium distribution 'msedge' is not found at /private/browser"),
            SourceErrorCode.BROWSER_NOT_INSTALLED,
        ),
        (
            "chrome",
            RuntimeError("Chromium distribution 'chrome' is not found at /private/browser"),
            SourceErrorCode.BROWSER_NOT_INSTALLED,
        ),
        ("chromium", RuntimeError("private launch details"), SourceErrorCode.BROWSER_START_FAILED),
        (
            "chromium",
            NotImplementedError("private loop details"),
            SourceErrorCode.BROWSER_SUBPROCESS_UNSUPPORTED,
        ),
    ],
)
def test_browser_startup_error_is_classified_without_exposing_stderr(
    monkeypatch, channel, failure, expected
):
    manager = Mock()
    manager.chromium.launch.side_effect = failure
    api = Mock()
    api.sync_playwright.return_value.start.return_value = manager
    monkeypatch.setattr(
        "vinted_deal_hunter.vinted_playwright.importlib.import_module", Mock(return_value=api)
    )
    with pytest.raises(SourceError) as caught:
        _PlaywrightBrowserSession(VintedPlaywrightConfig(browser_channel=channel))
    assert caught.value.code == expected
    assert "private" not in str(caught.value)
    manager.stop.assert_called_once()


def test_driver_start_failure_preserved_without_manager(monkeypatch):
    api = Mock()
    api.sync_playwright.return_value.start.side_effect = NotImplementedError()
    monkeypatch.setattr(
        "vinted_deal_hunter.vinted_playwright.importlib.import_module", Mock(return_value=api)
    )
    with pytest.raises(SourceError) as caught:
        _PlaywrightBrowserSession(VintedPlaywrightConfig())
    assert caught.value.code == SourceErrorCode.BROWSER_SUBPROCESS_UNSUPPORTED


def test_cleanup_failure_does_not_mask_startup_diagnosis(monkeypatch, caplog):
    api = Mock()
    manager = api.sync_playwright.return_value.start.return_value
    manager.chromium.launch.side_effect = RuntimeError("Executable doesn't exist at /private")
    manager.stop.side_effect = RuntimeError("private cleanup details")
    monkeypatch.setattr(
        "vinted_deal_hunter.vinted_playwright.importlib.import_module", Mock(return_value=api)
    )
    with pytest.raises(SourceError) as caught:
        _PlaywrightBrowserSession(VintedPlaywrightConfig())
    assert caught.value.code == SourceErrorCode.CHROMIUM_NOT_INSTALLED
    assert "private" not in caplog.text


def item_html(item_id: str, price: str = "25.50") -> bytes:
    return f'''<html><head><script type="application/ld+json">{{
      "@type":"Product", "name":"DEMO rendered shoe {item_id}", "brand":"DEMO",
      "offers":{{"@type":"Offer", "price":"{price}", "priceCurrency":"PLN",
      "url":"{ORIGIN}/items/{item_id}-demo"}}
    }}</script></head></html>'''.encode()


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeBrowser:
    def __init__(self, documents: dict[str, list[BrowserDocument | Exception]]) -> None:
        self.documents = documents
        self.calls: list[tuple[str, bool]] = []
        self.closed = False

    def navigate(self, url: str, *, catalog: bool) -> BrowserDocument:
        self.calls.append((url, catalog))
        result = self.documents[url].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def document(
    url: str,
    *,
    status: int = 200,
    final_url: str | None = None,
    html: bytes | None = None,
    links: tuple[str, ...] = (),
    title: str = "Vinted",
) -> BrowserDocument:
    return BrowserDocument(
        status_code=status,
        requested_url=url,
        final_url=final_url or url,
        title=title,
        html=html if html is not None else b"<html></html>",
        links=links,
    )


def robots_transport(document: str = "User-agent: *\nAllow: /") -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, text=document, headers={"Content-Type": "text/plain"})
    )


def source_for(
    urls: list[str], browser: FakeBrowser, clock: FakeTime, **config: object
) -> VintedPlaywrightOfferSource:
    return VintedPlaywrightOfferSource(
        urls,
        VintedPlaywrightConfig(**config),
        robots_transport=robots_transport(),
        session_factory=lambda _: browser,
        sleep=clock.sleep,
        clock=clock.clock,
    )


def test_rendered_catalog_collects_visible_links_and_imports_prices(settings, repository):
    catalog = search_url("Nike Air Max")
    browser = FakeBrowser(
        {
            catalog: [
                document(
                    catalog,
                    links=(
                        ITEM_123,
                        ITEM_123 + "?tracking=1",
                        ITEM_456,
                        "https://evil.example/items/999-demo",
                        f"{ORIGIN}/api/v2/items/888",
                    ),
                )
            ],
            ITEM_123: [document(ITEM_123, html=item_html("123"))],
            ITEM_456: [document(ITEM_456, html=item_html("456", "40.00"))],
        }
    )
    clock = FakeTime()
    source = source_for([catalog], browser, clock, max_pages=3)
    report = import_offers(source, repository, settings)

    assert (report.new, report.skipped, report.errors) == (2, 0, 0)
    assert [offer.asking_price for offer in repository.list_offers()] == [
        Decimal("25.50"),
        Decimal("40.00"),
    ]
    assert browser.calls == [(catalog, True), (ITEM_123, False), (ITEM_456, False)]
    assert browser.closed
    assert clock.sleeps == [5, 5, 5]
    assert source.report.engine == "playwright"
    assert source.report.requests == 4  # robots plus three top-level navigations
    assert [page.status for page in source.report.pages] == [
        "catalog",
        "collected",
        "collected",
    ]


def test_browser_retry_is_bounded_and_uses_backoff():
    browser = FakeBrowser(
        {
            ITEM_123: [
                BrowserNavigationError("timeout"),
                document(ITEM_123, html=item_html("123")),
            ]
        }
    )
    clock = FakeTime()
    source = source_for([ITEM_123], browser, clock)
    assert len(source.read(max_records=1)) == 1
    assert clock.sleeps == [5, 7]
    assert source.report.requests == 3


@pytest.mark.parametrize(
    "blocked",
    [
        document(ITEM_123, status=403),
        document(ITEM_123, status=429),
        document(ITEM_123, final_url=f"{ORIGIN}/catalog"),
        document(ITEM_123, title="Just a moment..."),
    ],
)
def test_access_restriction_redirect_or_challenge_stops_without_import(
    blocked, settings, repository
):
    browser = FakeBrowser({ITEM_123: [blocked]})
    source = source_for([ITEM_123], browser, FakeTime())
    with pytest.raises(CollectionStopped):
        import_offers(source, repository, settings)
    assert repository.list_offers() == []
    assert browser.closed
    assert source.report.stopped_reason


def test_robots_blocks_before_browser_is_created():
    created = False

    def factory(_):
        nonlocal created
        created = True
        raise AssertionError("browser must not start")

    source = VintedPlaywrightOfferSource(
        [ITEM_123],
        robots_transport=robots_transport("User-agent: *\nDisallow: /items/"),
        session_factory=factory,
    )
    with pytest.raises(CollectionStopped, match="robots_disallowed"):
        source.read(max_records=1)
    assert not created


def test_bad_rendered_metadata_is_a_page_error():
    browser = FakeBrowser({ITEM_123: [document(ITEM_123)]})
    source = source_for([ITEM_123], browser, FakeTime())
    assert source.read(max_records=1) == []
    assert source.report.pages[0].status == "error"
    assert source.report.pages[0].reason == "Saved HTML contains no supported offer metadata"


def test_top_level_navigation_limit_stops_before_next_page():
    browser = FakeBrowser(
        {
            ITEM_123: [document(ITEM_123, html=item_html("123"))],
            ITEM_456: [document(ITEM_456, html=item_html("456"))],
        }
    )
    source = source_for([ITEM_123, ITEM_456], browser, FakeTime(), max_requests=2)
    with pytest.raises(CollectionStopped, match="request_limit"):
        source.read(max_records=10)
    assert browser.calls == [(ITEM_123, False)]


def test_playwright_cli_is_default_and_emits_engine_report(tmp_path, capsys, monkeypatch, settings):
    browser = FakeBrowser({ITEM_123: [document(ITEM_123, html=item_html("123"))]})

    def offline(urls, config):
        return VintedPlaywrightOfferSource(
            urls,
            config,
            robots_transport=robots_transport(),
            session_factory=lambda _: browser,
            sleep=lambda _: None,
        )

    monkeypatch.setattr(vinted_scraper, "VintedPlaywrightOfferSource", offline)
    result = vinted_scraper.main(["--url", ITEM_123, "--db", str(tmp_path / "offers")])
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["collection"]["engine"] == "playwright"
    assert payload["import"]["new"] == 1


def test_cli_uses_browser_response_limit_independent_of_import_file_limit(
    tmp_path, capsys, monkeypatch
):
    captured = None
    browser = FakeBrowser({ITEM_123: [document(ITEM_123, html=item_html("123"))]})

    def offline(urls, config):
        nonlocal captured
        captured = config
        return VintedPlaywrightOfferSource(
            urls,
            config,
            robots_transport=robots_transport(),
            session_factory=lambda _: browser,
            sleep=lambda _: None,
        )

    monkeypatch.setenv("VDH_MAX_IMPORT_BYTES", "20")
    monkeypatch.setattr(vinted_scraper, "VintedPlaywrightOfferSource", offline)

    result = vinted_scraper.main(["--url", ITEM_123, "--db", str(tmp_path / "offers.sqlite3")])

    assert result == 0
    assert captured is not None
    assert captured.max_response_bytes == 10_000_000
    assert json.loads(capsys.readouterr().out)["import"]["new"] == 1


def test_http_only_options_are_rejected(capsys):
    with pytest.raises(SystemExit) as exc:
        vinted_scraper.main(["--url", ITEM_123, "--engine", "http", "--headed"])
    assert exc.value.code == 2
    assert "require --engine playwright" in capsys.readouterr().err


def test_cli_lock_prevents_overlapping_browser_runs(tmp_path, capsys, monkeypatch):
    browser = FakeBrowser({ITEM_123: [document(ITEM_123, html=item_html("123"))]})

    def offline(urls, config):
        return VintedPlaywrightOfferSource(
            urls,
            config,
            robots_transport=robots_transport(),
            session_factory=lambda _: browser,
            sleep=lambda _: None,
        )

    monkeypatch.setattr(vinted_scraper, "VintedPlaywrightOfferSource", offline)
    db = tmp_path / "offers.sqlite3"
    with portalocker.Lock(str(db) + ".vinted.lock", mode="a", timeout=0):
        result = vinted_scraper.main(["--url", ITEM_123, "--db", str(db)])
    assert result == 2
    assert browser.calls == []
    assert json.loads(capsys.readouterr().out)["error"] == "collection_or_import_failed"


def test_environment_configures_scheduled_playwright_source(monkeypatch):
    monkeypatch.setenv(
        "VDH_VINTED",
        json.dumps(
            {
                "enabled": True,
                "search": "  Nike   Air Max  ",
                "browser": {
                    "max_pages": 4,
                    "max_requests": 8,
                    "browser_channel": "msedge",
                },
            }
        ),
    )
    settings = Settings.from_env()
    source = configured_vinted_source(settings.vinted)
    assert settings.vinted.search == "Nike Air Max"
    assert isinstance(source, VintedPlaywrightOfferSource)
    assert source.urls == [search_url("Nike Air Max")]
    assert source.config.browser_channel == "msedge"


def test_environment_can_select_http_fallback():
    config = VintedAutomationConfig(
        enabled=True,
        engine="http",
        urls=(ITEM_123,),
        browser=VintedPlaywrightConfig(max_pages=1),
    )
    source = configured_vinted_source(config)
    assert isinstance(source, VintedOfferSource)
    assert configured_vinted_source(VintedAutomationConfig()) is None


def test_runtime_uses_configured_source_and_default_remains_demo(settings, repository):
    configured = settings.model_copy(
        update={"vinted": VintedAutomationConfig(enabled=True, search="Nike")}
    )
    runtime = JobRuntime(configured, repository)
    try:
        assert isinstance(runtime.manager.cycle.source, VintedPlaywrightOfferSource)
        assert not runtime.manager.cycle.source.is_demo
    finally:
        asyncio.run(runtime.close())

    demo_runtime = JobRuntime(settings, repository)
    try:
        assert demo_runtime.manager.cycle.source.is_demo
        assert demo_runtime.manager.cycle.source.source_name == "demo"
    finally:
        asyncio.run(demo_runtime.close())


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": True},
        {"enabled": True, "search": "shoe", "urls": [ITEM_123]},
        {
            "enabled": True,
            "urls": [ITEM_123, ITEM_456],
            "browser": {"max_pages": 1},
        },
        {"enabled": True, "urls": ["https://evil.example/items/123"]},
    ],
)
def test_invalid_scheduled_source_configuration_is_rejected(values):
    with pytest.raises(ValueError):
        VintedAutomationConfig.model_validate(values)


@pytest.mark.parametrize(
    ("url", "resource_type", "allowed"),
    [
        ("https://www.vinted.pl/catalog", "document", True),
        ("https://static.vinted.com/app.js", "script", True),
        ("https://www.vinted.pl/api/v2/items", "document", False),
        ("https://www.vinted.pl/catalog", "xhr", False),
        ("https://tracker.example/script.js", "script", False),
    ],
)
def test_browser_route_blocks_private_and_third_party_requests(url, resource_type, allowed):
    class Route:
        action = ""

        def abort(self):
            self.action = "abort"

        def continue_(self):
            self.action = "continue"

    class Request:
        pass

    route = Route()
    request = Request()
    request.url = url
    request.resource_type = resource_type
    _PlaywrightBrowserSession._route(route, request)
    assert route.action == ("continue" if allowed else "abort")
