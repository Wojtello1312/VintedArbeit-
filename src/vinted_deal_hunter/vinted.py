"""Opt-in, bounded collection of asking prices from public Vinted Poland HTML."""

import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vinted_deal_hunter.models import OfferInput
from vinted_deal_hunter.sources import SavedHTMLOfferSource, SourceError

ORIGIN = "https://www.vinted.pl"
USER_AGENT = "VintedDealHunterTest/0.1"
ITEM_PATH = re.compile(r"^/items/([0-9]+)(?:-[^/?#]*)?/?$")
logger = logging.getLogger(__name__)


class VintedScrapeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_pages: int = Field(default=10, ge=1, le=100)
    max_requests: int = Field(default=25, ge=2, le=250)
    interval_seconds: float = Field(default=5, ge=1, le=300)
    timeout_seconds: float = Field(default=15, gt=0, le=60)
    max_response_bytes: int = Field(default=10_000_000, ge=1, le=10_000_000)
    retries: int = Field(default=1, ge=0, le=3)
    backoff_seconds: float = Field(default=2, ge=1, le=30)


class PageResult(BaseModel):
    url: str
    status: Literal["collected", "catalog", "error"]
    observed_at: datetime
    reason: str | None = None
    offer: OfferInput | None = None
    price_basis: Literal["ASKING"] | None = None


class CollectionReport(BaseModel):
    source_name: str = "vinted-public-pl"
    engine: Literal["http", "playwright"] = "http"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    requests: int = 0
    pages: list[PageResult] = Field(default_factory=list)
    truncated: bool = False
    stopped_reason: str | None = None


class CollectionStopped(SourceError):
    """Stop the whole run on access restrictions or exhausted request budgets."""


def challenge_detected(content: bytes, title: str = "") -> bool:
    lowered = content.lower()
    normalized_title = title.strip().casefold()
    return normalized_title in {"just a moment...", "access denied", "captcha"} or any(
        marker in lowered
        for marker in (
            b"captcha-delivery.com",
            b"<title>just a moment",
            b"<title>access denied",
            b"<title>captcha",
        )
    )


def public_url(value: str) -> str:
    """Allow only public catalog/item URLs; never accept credentials or API paths."""
    try:
        parsed = urlsplit(value)
        valid = (
            len(value) <= 2000
            and not any(ord(char) <= 32 for char in value)
            and parsed.scheme == "https"
            and parsed.hostname in {"www.vinted.pl", "vinted.pl"}
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and (parsed.path in {"/catalog", "/catalog/"} or ITEM_PATH.fullmatch(parsed.path))
        )
    except ValueError:
        valid = False
    if not valid:
        raise SourceError("Expected a public https://www.vinted.pl/items/... or /catalog URL")
    # Strip item tracking parameters; preserve catalog filters explicitly supplied by the user.
    query = parsed.query if parsed.path.startswith("/catalog") else ""
    return urlunsplit(("https", "www.vinted.pl", parsed.path, query, ""))


def search_url(query: str) -> str:
    return f"{ORIGIN}/catalog?{urlencode({'search_text': query})}"


def parse_offer_html(content: bytes, url: str) -> OfferInput:
    rows = SavedHTMLOfferSource(content, source_name="vinted-public-pl", is_demo=False).read(
        max_records=10
    )
    match = ITEM_PATH.fullmatch(urlsplit(url).path)
    if match is None:
        raise SourceError("not_an_item_url")
    matching = [row for row in rows if isinstance(row, dict) and row.get("external_id") == match[1]]
    if len(matching) != 1:
        raise SourceError("ambiguous_or_mismatched_product_metadata")
    try:
        return OfferInput.model_validate(
            {**matching[0], "source_name": "vinted-public-pl", "is_demo": False}
        )
    except ValidationError as exc:
        raise SourceError("invalid_offer_metadata") from exc


def robots_allowed(document: str, url: str) -> bool:
    """Longest matching rule wins, with '*' and terminal '$' support.

    urllib.robotparser provides crawl delays but does not implement wildcards.
    Combine equally specific user-agent groups; Allow wins equal-length ties.
    """
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    has_directive = False
    for line in [*document.splitlines(), "User-agent: __end__"]:
        name, separator, value = line.split("#", 1)[0].partition(":")
        if not separator:
            continue
        name, value = name.strip().lower(), value.strip()
        if name == "user-agent":
            if has_directive:
                groups.append((agents, rules))
                agents, rules, has_directive = [], [], False
            agents.append(value.lower())
        elif agents:
            has_directive = True
            if name in {"allow", "disallow"} and value:
                rules.append((name, value))
    selected: list[tuple[str, str]] = []
    specificity = -1
    for names, entries in groups:
        score = max(
            (
                0 if name == "*" else len(name)
                for name in names
                if name == "*" or name in USER_AGENT.lower()
            ),
            default=-1,
        )
        if score >= 0 and score >= specificity:
            selected = selected + entries if score == specificity else entries.copy()
            specificity = score
    parsed = urlsplit(url)
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    matches: list[tuple[int, bool]] = []
    for kind, pattern in selected:
        end = "$" if pattern.endswith("$") else ""
        pattern = pattern[:-1] if end else pattern
        expression = "^" + ".*".join(re.escape(part) for part in pattern.split("*")) + end
        if re.search(expression, target):
            matches.append((len(pattern.replace("*", "")), kind == "allow"))
    return max(matches)[1] if matches else True


class _CatalogLinks(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.links: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or len(self.links) >= self.limit:
            return
        href = dict(attrs).get("href")
        if href is None:
            return
        try:
            url = public_url(urljoin(ORIGIN, href))
        except SourceError:
            return
        match = ITEM_PATH.fullmatch(urlsplit(url).path)
        if match:
            self.links.setdefault(match.group(1), url)


class VintedOfferSource:
    """OfferSource implementation. Network I/O happens only inside read().

    Inject transport/clock/sleep for offline tests. Each read is a bounded run;
    rate limiting is per instance, not a distributed or cross-process guarantee.
    """

    source_name = "vinted-public-pl"
    is_demo = False

    def __init__(
        self,
        urls: Sequence[str],
        config: VintedScrapeConfig | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or VintedScrapeConfig()
        if not urls or len(urls) > self.config.max_pages:
            raise SourceError("Supply between one and max_pages seed URLs")
        self.urls = list(dict.fromkeys(public_url(url) for url in urls))
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self.report = CollectionReport()
        self._next_request = 0.0
        self._interval = self.config.interval_seconds

    def _request(self, client: httpx.Client, url: str, *, robots: bool = False) -> bytes:
        for attempt in range(self.config.retries + 1):
            if self.report.requests >= self.config.max_requests:
                raise CollectionStopped("request_limit")
            delay = self._next_request - self.clock()
            if delay > 0:
                self.sleep(delay)
            self.report.requests += 1
            client.cookies.clear()
            retry = False
            try:
                with client.stream("GET", url) as response:
                    code = response.status_code
                    if code in {401, 403, 429}:
                        raise CollectionStopped(f"access_restricted_http_{code}")
                    if 300 <= code < 400:
                        raise CollectionStopped("redirect_requires_manual_review")
                    if code >= 500:
                        if "retry-after" in response.headers:
                            raise CollectionStopped("server_requested_retry_after")
                        retry = True
                    elif code != 200:
                        raise SourceError(f"http_{code}")
                    else:
                        expected = "text/plain" if robots else "text/html"
                        actual = response.headers.get("content-type", "").split(";", 1)[0]
                        if actual.strip().lower() != expected:
                            raise SourceError("unexpected_content_type")
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=8192):
                            if len(body) + len(chunk) > self.config.max_response_bytes:
                                raise SourceError("response_size_limit")
                            body.extend(chunk)
                        return bytes(body)
            except httpx.RequestError:
                retry = True
            finally:
                self._next_request = self.clock() + self._interval
            if retry and attempt < self.config.retries:
                self._next_request += self.config.backoff_seconds * (2**attempt)
                continue
            raise SourceError("transient_failure_retries_exhausted")
        raise AssertionError("Unreachable request attempt")

    def _robots(self, client: httpx.Client) -> str:
        try:
            body = self._request(client, f"{ORIGIN}/robots.txt", robots=True)
            text = body.decode("utf-8-sig")
            if "<html" in text.casefold() or "<script" in text.casefold():
                raise SourceError("robots_is_html")
        except (SourceError, UnicodeError) as exc:
            if isinstance(exc, CollectionStopped):
                raise
            raise CollectionStopped("robots_unavailable") from exc
        rules = RobotFileParser()
        rules.parse(text.splitlines())
        # An empty robots file can mean allow-all; a challenge page must not.
        if text.strip() and not any(
            line.strip().lower().startswith("user-agent:") for line in text.splitlines()
        ):
            raise CollectionStopped("unrecognized_robots_document")
        self._interval = max(
            self.config.interval_seconds, float(rules.crawl_delay(USER_AGENT) or 0)
        )
        rate = rules.request_rate(USER_AGENT)
        if rate and rate.requests > 0:
            self._interval = max(self._interval, rate.seconds / rate.requests)
        self._next_request = max(self._next_request, self.clock() + self._interval)
        return text

    def _offer(self, body: bytes, url: str) -> OfferInput:
        return parse_offer_html(body, url)

    def read(self, *, max_records: int) -> list[object]:
        if max_records < 1:
            raise SourceError("max_records must be positive")
        self.report = CollectionReport()
        self._interval = self.config.interval_seconds
        records: list[object] = []
        queue = list(self.urls)
        seen: set[str] = set()
        with httpx.Client(
            transport=self.transport,
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"},
        ) as client:
            try:
                rules = self._robots(client)
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
                        body = self._request(client, url)
                        if challenge_detected(body):
                            raise CollectionStopped("challenge_requires_manual_review")
                        if match:
                            offer = self._offer(body, url)
                            records.append(offer.model_dump(exclude={"source_name", "is_demo"}))
                            page = PageResult(
                                url=url,
                                status="collected",
                                observed_at=datetime.now(UTC),
                                offer=offer,
                                price_basis="ASKING",
                            )
                        else:
                            links = _CatalogLinks(self.config.max_pages + 1)
                            links.feed(body.decode("utf-8-sig"))
                            links.close()
                            if not links.links:
                                raise SourceError("no_item_links_in_html")
                            queue.extend(links.links.values())
                            page = PageResult(
                                url=url, status="catalog", observed_at=datetime.now(UTC)
                            )
                        self.report.pages.append(page)
                    except CollectionStopped:
                        raise
                    except (SourceError, UnicodeError, ValueError) as exc:
                        # Never persist raw HTML or exception contents (may contain private data).
                        self.report.pages.append(
                            PageResult(
                                url=url,
                                status="error",
                                observed_at=datetime.now(UTC),
                                reason=str(exc) if isinstance(exc, SourceError) else "invalid_html",
                            )
                        )
                        logger.warning("Vinted page could not be imported (page %d)", len(seen))
            except CollectionStopped as exc:
                self.report.stopped_reason = str(exc)
                raise
            finally:
                self.report.finished_at = datetime.now(UTC)
        return records
