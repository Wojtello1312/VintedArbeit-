"""Browser-backed Vinted collector using an isolated Playwright context."""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vinted_deal_hunter.collection_filters import CollectionFilters
from vinted_deal_hunter.sources import SourceError
from vinted_deal_hunter.vinted import (
    ITEM_PATH,
    ORIGIN,
    USER_AGENT,
    CollectionReport,
    CollectionStopped,
    PageResult,
    VintedScrapeConfig,
    challenge_detected,
    parse_offer_html,
    public_url,
    robots_allowed,
    search_url,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vinted_deal_hunter.vinted import VintedOfferSource


class VintedPlaywrightConfig(VintedScrapeConfig):
    headless: bool = True
    browser_channel: Literal["chromium", "chrome", "msedge"] = "chromium"
    browser_executable: Path | None = None
    render_wait_seconds: float = Field(default=1, ge=0, le=10)
    scroll_rounds: int = Field(default=3, ge=0, le=20)
    scroll_wait_seconds: float = Field(default=1, ge=0.1, le=10)


class VintedAutomationConfig(BaseModel):
    """Environment-safe composition for optional scheduled collection."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    engine: Literal["http", "playwright"] = "playwright"
    search: str | None = Field(default=None, max_length=200)
    urls: tuple[str, ...] = Field(default=(), max_length=100)
    browser: VintedPlaywrightConfig = Field(default_factory=VintedPlaywrightConfig)
    filters: CollectionFilters | None = None

    @field_validator("search")
    @classmethod
    def clean_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @model_validator(mode="after")
    def validate_seeds(self) -> Self:
        if self.enabled and bool(self.search) == bool(self.urls):
            raise ValueError("enabled Vinted source requires exactly one of search or urls")
        if len(self.urls) > self.browser.max_pages:
            raise ValueError("Vinted seed URL count exceeds max_pages")
        for url in self.urls:
            public_url(url)
        return self


@dataclass(frozen=True)
class BrowserDocument:
    status_code: int
    requested_url: str
    final_url: str
    title: str
    html: bytes
    links: tuple[str, ...] = ()


class BrowserNavigationError(SourceError):
    """A browser navigation failed before a trustworthy document was available."""


class BrowserSession(Protocol):
    def navigate(self, url: str, *, catalog: bool) -> BrowserDocument: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


SessionFactory = Callable[[VintedPlaywrightConfig], BrowserSession]


class _PlaywrightBrowserSession:
    """Thin dynamic wrapper so the base install stays Playwright-free."""

    def __init__(self, config: VintedPlaywrightConfig) -> None:
        self.config = config
        try:
            api: Any = importlib.import_module("playwright.sync_api")
        except ImportError as exc:
            raise SourceError(
                "playwright_not_installed: run uv sync --locked --extra browser"
            ) from exc
        try:
            self._manager: Any = api.sync_playwright().start()
            launch_options: dict[str, object] = {"headless": config.headless}
            if config.browser_executable is not None:
                launch_options["executable_path"] = str(config.browser_executable)
            elif config.browser_channel != "chromium":
                launch_options["channel"] = config.browser_channel
            self._browser: Any = self._manager.chromium.launch(**launch_options)
            self._context: Any = self._browser.new_context(
                locale="pl-PL",
                timezone_id="Europe/Warsaw",
                user_agent=USER_AGENT,
                service_workers="block",
            )
            self._context.route("**/*", self._route)
            self._page: Any = self._context.new_page()
            self._page.set_default_timeout(config.timeout_seconds * 1000)
        except Exception as exc:
            manager = getattr(self, "_manager", None)
            if manager is not None:
                manager.stop()
            hint = (
                "run uv run --extra browser playwright install chromium"
                if config.browser_channel == "chromium" and config.browser_executable is None
                else f"install or select browser channel {config.browser_channel}"
            )
            raise SourceError(f"browser_unavailable: {hint}") from exc

    @staticmethod
    def _route(route: Any, request: Any) -> None:
        """Keep navigation public and light; do not call private data endpoints."""
        parsed = urlsplit(str(request.url))
        path = parsed.path.casefold()
        host = (parsed.hostname or "").casefold()
        first_party = host in {"vinted.pl", "vinted.com", "vinted.net"} or host.endswith(
            (".vinted.pl", ".vinted.com", ".vinted.net")
        )
        blocked_type = str(request.resource_type) in {
            "fetch",
            "font",
            "image",
            "media",
            "websocket",
            "xhr",
        }
        if not first_party or blocked_type or path.startswith("/api/") or "/api/v" in path:
            route.abort()
        else:
            route.continue_()

    def navigate(self, url: str, *, catalog: bool) -> BrowserDocument:
        try:
            response: Any = self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.config.timeout_seconds * 1000,
            )
            if response is None:
                raise BrowserNavigationError("navigation_without_response")
            self._page.wait_for_timeout(self.config.render_wait_seconds * 1000)
            if catalog:
                for _ in range(self.config.scroll_rounds):
                    self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    self._page.wait_for_timeout(self.config.scroll_wait_seconds * 1000)
            html = self._page.content().encode("utf-8")
            if len(html) > self.config.max_response_bytes:
                raise SourceError("response_size_limit")
            hrefs: list[str] = (
                self._page.locator("a[href*='/items/']").evaluate_all(
                    "elements => elements.map(element => element.href)"
                )
                if catalog
                else []
            )
            return BrowserDocument(
                status_code=int(response.status),
                requested_url=url,
                final_url=str(self._page.url),
                title=str(self._page.title()),
                html=html,
                links=tuple(str(href) for href in hrefs),
            )
        except SourceError:
            raise
        except Exception as exc:
            raise BrowserNavigationError("browser_navigation_failed") from exc

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._manager.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _default_session_factory(config: VintedPlaywrightConfig) -> BrowserSession:
    return _PlaywrightBrowserSession(config)


class VintedPlaywrightOfferSource:
    """Automate public catalog/item navigation and feed validated HTML to ingestion."""

    source_name = "vinted-public-pl"
    is_demo = False

    def __init__(
        self,
        urls: Sequence[str],
        config: VintedPlaywrightConfig | None = None,
        *,
        robots_transport: httpx.BaseTransport | None = None,
        session_factory: SessionFactory = _default_session_factory,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or VintedPlaywrightConfig()
        if not urls or len(urls) > self.config.max_pages:
            raise SourceError("Supply between one and max_pages seed URLs")
        self.urls = list(dict.fromkeys(public_url(url) for url in urls))
        self.robots_transport = robots_transport
        self.session_factory = session_factory
        self.sleep = sleep
        self.clock = clock
        self.report = CollectionReport(engine="playwright")
        self.filters: CollectionFilters | None = None
        self.progress: Callable[[CollectionReport], None] | None = None
        self._interval = self.config.interval_seconds
        self._next_navigation = 0.0

    def _robots(self) -> str:
        if self.report.requests >= self.config.max_requests:
            raise CollectionStopped("request_limit")
        self.report.requests += 1
        try:
            with httpx.Client(
                transport=self.robots_transport,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
            ) as client:
                response = client.get(f"{ORIGIN}/robots.txt")
            if response.status_code in {401, 403, 429}:
                raise CollectionStopped(f"access_restricted_http_{response.status_code}")
            if response.status_code != 200:
                raise SourceError(f"http_{response.status_code}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if content_type.strip().casefold() != "text/plain":
                raise SourceError("unexpected_robots_content_type")
            if len(response.content) > self.config.max_response_bytes:
                raise SourceError("robots_size_limit")
            document = response.content.decode("utf-8-sig")
            if document.strip() and not any(
                line.strip().casefold().startswith("user-agent:") for line in document.splitlines()
            ):
                raise SourceError("unrecognized_robots_document")
        except CollectionStopped:
            raise
        except (httpx.RequestError, SourceError, UnicodeError) as exc:
            raise CollectionStopped("robots_unavailable") from exc
        parser = RobotFileParser()
        parser.parse(document.splitlines())
        self._interval = max(
            self.config.interval_seconds, float(parser.crawl_delay(USER_AGENT) or 0)
        )
        rate = parser.request_rate(USER_AGENT)
        if rate and rate.requests > 0:
            self._interval = max(self._interval, rate.seconds / rate.requests)
        self._next_navigation = self.clock() + self._interval
        return document

    def _wait_for_slot(self) -> None:
        if self.report.requests >= self.config.max_requests:
            raise CollectionStopped("request_limit")
        delay = self._next_navigation - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.report.requests += 1
        self._next_navigation = self.clock() + self._interval

    def _navigate(self, session: BrowserSession, url: str, *, catalog: bool) -> BrowserDocument:
        for attempt in range(self.config.retries + 1):
            self._wait_for_slot()
            try:
                document = session.navigate(url, catalog=catalog)
            except BrowserNavigationError:
                if attempt < self.config.retries:
                    self._next_navigation += self.config.backoff_seconds * (2**attempt)
                    continue
                raise SourceError("browser_retries_exhausted") from None
            if document.status_code in {401, 403, 429}:
                raise CollectionStopped(f"access_restricted_http_{document.status_code}")
            if document.status_code >= 500:
                if attempt < self.config.retries:
                    self._next_navigation += self.config.backoff_seconds * (2**attempt)
                    continue
                raise SourceError("browser_retries_exhausted")
            if document.status_code != 200:
                raise SourceError(f"http_{document.status_code}")
            if public_url(document.final_url) != url:
                raise CollectionStopped("redirect_requires_manual_review")
            if challenge_detected(document.html, document.title):
                raise CollectionStopped("challenge_requires_manual_review")
            return document
        raise AssertionError("Unreachable browser navigation attempt")

    def read(self, *, max_records: int) -> list[object]:
        if max_records < 1:
            raise SourceError("max_records must be positive")
        self.report = CollectionReport(engine="playwright")
        self._interval = self.config.interval_seconds
        records: list[object] = []
        queue = list(self.urls)
        seen: set[str] = set()
        try:
            rules = self._robots()
            for seed_url in self.urls:
                if not robots_allowed(rules, seed_url):
                    raise CollectionStopped("robots_disallowed")
            with self.session_factory(self.config) as session:
                while queue:
                    if (
                        len(self.report.pages) >= self.config.max_pages
                        or len(records) >= max_records
                    ):
                        self.report.truncated = True
                        break
                    url = queue.pop(0)
                    match = ITEM_PATH.fullmatch(urlsplit(url).path)
                    key = match[1] if match else url
                    if key in seen:
                        continue
                    seen.add(key)
                    if not robots_allowed(rules, url):
                        raise CollectionStopped("robots_disallowed")
                    try:
                        document = self._navigate(session, url, catalog=match is None)
                        if match:
                            offer = parse_offer_html(document.html, url)
                            rejection = self.filters.rejection(offer) if self.filters else None
                            if rejection is None:
                                records.append(offer.model_dump(exclude={"source_name", "is_demo"}))
                            page = PageResult(
                                url=url,
                                status="excluded" if rejection else "collected",
                                reason=rejection,
                                observed_at=datetime.now(UTC),
                                offer=offer,
                                price_basis="ASKING",
                            )
                        else:
                            links: dict[str, str] = {}
                            for href in document.links:
                                try:
                                    item_url = public_url(urljoin(ORIGIN, href))
                                except SourceError:
                                    continue
                                item_match = ITEM_PATH.fullmatch(urlsplit(item_url).path)
                                if item_match:
                                    links.setdefault(item_match[1], item_url)
                            if not links:
                                raise SourceError("no_item_links_in_rendered_dom")
                            queue.extend(links.values())
                            page = PageResult(
                                url=url, status="catalog", observed_at=datetime.now(UTC)
                            )
                        self.report.pages.append(page)
                    except CollectionStopped:
                        raise
                    except (SourceError, UnicodeError, ValueError) as exc:
                        self.report.pages.append(
                            PageResult(
                                url=url,
                                status="error",
                                observed_at=datetime.now(UTC),
                                reason=str(exc) if isinstance(exc, SourceError) else "invalid_html",
                            )
                        )
                        logger.warning(
                            "Playwright Vinted page could not be imported (page %d)", len(seen)
                        )
                    if self.progress is not None:
                        self.progress(self.report)
        except CollectionStopped as exc:
            self.report.stopped_reason = str(exc)
            raise
        finally:
            self.report.finished_at = datetime.now(UTC)
        return records


def configured_vinted_source(
    config: VintedAutomationConfig,
) -> VintedPlaywrightOfferSource | VintedOfferSource | None:
    """Build the explicitly enabled source without starting network or browser I/O."""
    if not config.enabled:
        return None
    urls = [search_url(config.search)] if config.search is not None else list(config.urls)
    if config.filters is not None:
        filtered_urls = []
        for url in urls:
            parsed = urlsplit(url)
            if parsed.path.startswith("/catalog"):
                criteria = config.filters.search_parameters()
                params = [
                    (key, value)
                    for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                    if key not in criteria
                ]
                params.extend(criteria.items())
                url = urlunsplit(parsed._replace(query=urlencode(params)))
            filtered_urls.append(url)
        urls = filtered_urls
    if config.engine == "playwright":
        source = VintedPlaywrightOfferSource(urls, config.browser)
        source.filters = config.filters
        return source
    from vinted_deal_hunter.vinted import VintedOfferSource

    http_config = VintedScrapeConfig.model_validate(
        config.browser.model_dump(include=set(VintedScrapeConfig.model_fields))
    )
    http_source = VintedOfferSource(urls, http_config)
    http_source.filters = config.filters
    return http_source
