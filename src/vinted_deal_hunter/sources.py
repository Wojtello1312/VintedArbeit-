"""Local adapters and the extension contract for explicitly authorized sources."""

import csv
import io
import json
import re
from abc import ABC, abstractmethod
from decimal import Decimal
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse


class SourceError(ValueError):
    """The source could not be read safely; distinct from a bad individual record."""


class OfferSource(Protocol):
    @property
    def source_name(self) -> str: ...

    @property
    def is_demo(self) -> bool: ...

    def read(self, *, max_records: int) -> list[object]: ...


class AuthorizedOfferSource(ABC):
    """Extension only. Implementors must document permission, timeouts and limits.

    No Vinted adapter, networking or implicit authorization is provided here.
    """

    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @property
    def is_demo(self) -> bool:
        return False

    @abstractmethod
    def read(self, *, max_records: int) -> list[object]: ...


def read_local_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise SourceError("Cannot read the local import file") from exc
    if len(data) > max_bytes:
        raise SourceError("Import file exceeds the byte limit")
    return data


def decode_content(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("Import requires UTF-8 encoding") from exc


class JSONOfferSource:
    def __init__(self, content: bytes, *, source_name: str, is_demo: bool) -> None:
        self.content = content
        self.source_name = source_name
        self.is_demo = is_demo

    def read(self, *, max_records: int) -> list[object]:
        try:
            data: object = json.loads(decode_content(self.content), parse_float=Decimal)
        except (ValueError, RecursionError) as exc:
            raise SourceError("Invalid JSON document") from exc
        if not isinstance(data, list):
            raise SourceError("JSON document must be an array of offer records")
        if len(data) > max_records:
            raise SourceError("Import exceeds the record limit")
        return data


class CSVOfferSource:
    def __init__(self, content: bytes, *, source_name: str, is_demo: bool) -> None:
        self.content = content
        self.source_name = source_name
        self.is_demo = is_demo

    def read(self, *, max_records: int) -> list[object]:
        reader = csv.DictReader(io.StringIO(decode_content(self.content), newline=""), strict=True)
        records: list[object] = []
        try:
            headers = reader.fieldnames
            required = {"external_id", "product_name", "currency", "asking_price"}
            if not headers or len(set(headers)) != len(headers) or not required <= set(headers):
                raise SourceError("CSV requires unique headers and all required offer columns")
            for record in reader:
                if len(records) >= max_records:
                    raise SourceError("Import exceeds the record limit")
                # A short or overlong row is a bad record, not a source failure.
                if None in record or any(value is None for value in record.values()):
                    records.append(None)
                else:
                    records.append(record)
        except csv.Error as exc:
            raise SourceError("Malformed CSV document") from exc
        return records


class _OfferHTMLParser(HTMLParser):
    """Collect inert metadata without executing scripts or loading subresources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.metadata: dict[str, str] = {}
        self._json_ld_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs if value is not None}
        if tag.casefold() == "script":
            content_type = attributes.get("type", "").split(";", 1)[0].strip().casefold()
            if content_type == "application/ld+json":
                self._json_ld_parts = []
        elif tag.casefold() == "meta":
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if key and content:
                self.metadata.setdefault(key.strip().casefold(), content.strip())

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_ld_parts is not None:
            self.json_ld.append("".join(self._json_ld_parts))
            self._json_ld_parts = None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        result = value.strip()
        return result or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _schema_type(value: object, expected: str) -> bool:
    if isinstance(value, list):
        return any(_schema_type(item, expected) for item in value)
    text = _text(value)
    if text is None:
        return False
    return text.rstrip("/").rsplit("/", 1)[-1].casefold() == expected.casefold()


def _url_identifier(value: object) -> str | None:
    url = _text(value)
    if url is None or len(url) > 2000:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    match = re.search(r"(?:^|/)items/(\d+)(?:[-/]|$)", parsed.path, re.IGNORECASE)
    if match:
        return match.group(1)
    return url if len(url) <= 200 else None


def _identifier(*objects: dict[str, object]) -> str | None:
    for item in objects:
        for key in ("sku", "productID", "productId", "mpn"):
            value = _text(item.get(key))
            if value is not None and len(value) <= 200:
                return value
        value = _url_identifier(item.get("url") or item.get("@id"))
        if value is not None:
            return value
    return None


def _brand(value: object) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name"))
    return _text(value)


def _condition(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    label = text.rstrip("/").rsplit("/", 1)[-1].strip().casefold().replace("-", "_")
    explicit = {
        "new_with_tags": "new_with_tags",
        "nowy z metką": "new_with_tags",
        "new_without_tags": "new_without_tags",
        "nowy bez metki": "new_without_tags",
        "very_good": "very_good",
        "bardzo dobry": "very_good",
        "good": "good",
        "dobry": "good",
        "fair": "fair",
        "zadowalający": "fair",
        "unknown": "unknown",
        "nieznany": "unknown",
    }
    # Schema.org's NewCondition/UsedCondition cannot establish tags or wear grade.
    return explicit.get(label, "unknown")


def _price_and_currency(
    offer: dict[str, object], product: dict[str, object]
) -> tuple[object | None, str | None]:
    price: object | None = offer.get("price")
    currency = _text(offer.get("priceCurrency")) or _text(product.get("priceCurrency"))
    if isinstance(price, dict):
        currency = _text(price.get("currency")) or currency
        price = price.get("value")
    return price, currency


def _product_records(product: dict[str, object]) -> list[dict[str, object]]:
    name = _text(product.get("name"))
    if name is None:
        return []
    raw_offers = product.get("offers")
    offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
    records: list[dict[str, object]] = []
    for raw_offer in offers:
        if not isinstance(raw_offer, dict):
            continue
        if raw_offer.get("@type") is not None and not _schema_type(raw_offer.get("@type"), "Offer"):
            continue
        price, currency = _price_and_currency(raw_offer, product)
        external_id = _identifier(raw_offer, product)
        if price is None or currency is None or external_id is None:
            continue
        record: dict[str, object] = {
            "external_id": external_id,
            "product_name": name,
            "currency": currency,
            "asking_price": price,
        }
        optional: dict[str, object | None] = {
            "brand": _brand(product.get("brand")),
            "size": _text(product.get("size")),
            "condition": _condition(raw_offer.get("itemCondition") or product.get("itemCondition")),
            "published_at": _text(
                raw_offer.get("datePosted")
                or product.get("datePosted")
                or product.get("dateCreated")
                or product.get("uploadDate")
            ),
        }
        record.update({key: value for key, value in optional.items() if value is not None})
        records.append(record)
    return records


def _json_ld_records(documents: list[str], *, max_records: int) -> list[object]:
    records: list[object] = []
    for document in documents:
        try:
            root: object = json.loads(document, parse_float=Decimal)
        except (ValueError, RecursionError) as exc:
            raise SourceError("Invalid JSON-LD metadata in saved HTML") from exc
        pending: list[object] = [root]
        while pending:
            value = pending.pop()
            if isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, dict):
                if _schema_type(value.get("@type"), "Product"):
                    records.extend(_product_records(value))
                    if len(records) > max_records:
                        raise SourceError("Import exceeds the record limit")
                else:
                    pending.extend(value.values())
    return records


def _open_graph_record(metadata: dict[str, str]) -> dict[str, object] | None:
    name = metadata.get("og:title")
    url = metadata.get("og:url")
    external_id = (
        metadata.get("product:retailer_item_id")
        or metadata.get("product:sku")
        or _url_identifier(url)
    )
    price = metadata.get("product:price:amount") or metadata.get("og:price:amount")
    currency = metadata.get("product:price:currency") or metadata.get("og:price:currency")
    if not all((name, external_id, price, currency)):
        return None
    record: dict[str, object] = {
        "external_id": external_id,
        "product_name": name,
        "currency": currency,
        "asking_price": price,
    }
    optional: dict[str, object | None] = {
        "brand": metadata.get("product:brand"),
        "size": metadata.get("product:size"),
        "condition": _condition(metadata.get("product:condition")),
        "published_at": metadata.get("article:published_time"),
    }
    record.update({key: value for key, value in optional.items() if value is not None})
    return record


class SavedHTMLOfferSource:
    """Parse a user-saved page; this adapter never performs network requests."""

    def __init__(self, content: bytes, *, source_name: str, is_demo: bool) -> None:
        self.content = content
        self.source_name = source_name
        self.is_demo = is_demo

    def read(self, *, max_records: int) -> list[object]:
        parser = _OfferHTMLParser()
        try:
            parser.feed(decode_content(self.content))
            parser.close()
        except (ValueError, RecursionError) as exc:
            raise SourceError("Malformed saved HTML document") from exc
        records = _json_ld_records(parser.json_ld, max_records=max_records)
        if not records:
            fallback = _open_graph_record(parser.metadata)
            if fallback is not None:
                records.append(fallback)
        if not records:
            raise SourceError("Saved HTML contains no supported offer metadata")
        if len(records) > max_records:
            raise SourceError("Import exceeds the record limit")
        return records


class MockOfferSource(JSONOfferSource):
    def __init__(self) -> None:
        super().__init__(
            files("vinted_deal_hunter").joinpath("data/demo_offers.json").read_bytes(),
            source_name="demo",
            is_demo=True,
        )
