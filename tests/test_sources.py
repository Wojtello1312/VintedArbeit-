import json
from decimal import Decimal
from pathlib import Path

import pytest

from vinted_deal_hunter.sources import (
    AuthorizedOfferSource,
    CSVOfferSource,
    JSONOfferSource,
    MockOfferSource,
    SavedHTMLOfferSource,
    SourceError,
    read_local_file,
)


def test_json_decimal_precision() -> None:
    source = JSONOfferSource(b'[{"asking_price":0.29}]', source_name="demo", is_demo=True)
    assert source.read(max_records=1) == [{"asking_price": Decimal("0.29")}]


@pytest.mark.parametrize("content", [b"{", b"{}", b"null", b"\xff"])
def test_json_source_failure(content: bytes) -> None:
    with pytest.raises(SourceError):
        JSONOfferSource(content, source_name="demo", is_demo=True).read(max_records=10)


def test_csv_bom_quotes_and_bad_row() -> None:
    content = (
        "\ufeffexternal_id,product_name,currency,asking_price\n"
        'a,"Demo, shoes",PLN,"12,30"\nshort,row\n'
        "b,Demo,EUR,25\n"
    ).encode()
    records = CSVOfferSource(content, source_name="demo", is_demo=True).read(max_records=10)
    assert records[0] == {
        "external_id": "a",
        "product_name": "Demo, shoes",
        "currency": "PLN",
        "asking_price": "12,30",
    }
    assert records[1] is None
    assert isinstance(records[2], dict)


@pytest.mark.parametrize(
    "content",
    [b"", b"a,a\n1,2", b'external_id,product_name,currency,asking_price\na,"unterminated'],
)
def test_csv_structural_failure(content: bytes) -> None:
    with pytest.raises(SourceError):
        CSVOfferSource(content, source_name="demo", is_demo=True).read(max_records=10)


def test_limits_and_io(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        MockOfferSource().read(max_records=1)
    with pytest.raises(SourceError):
        CSVOfferSource(
            b"external_id,product_name,currency,asking_price\na,b,PLN,1\nc,d,PLN,2",
            source_name="demo",
            is_demo=True,
        ).read(max_records=1)
    path = tmp_path / "test.json"
    path.write_bytes(b"[]")
    assert read_local_file(path, max_bytes=2) == b"[]"
    with pytest.raises(SourceError):
        read_local_file(path, max_bytes=1)
    with pytest.raises(SourceError):
        read_local_file(tmp_path / "absent", max_bytes=2)


def test_authorized_source_has_no_connection_implementation() -> None:
    with pytest.raises(TypeError):
        AuthorizedOfferSource()  # type: ignore[abstract]
    assert len(MockOfferSource().read(max_records=1000)) >= 100


def test_non_objects_remain_record_errors() -> None:
    source = JSONOfferSource(
        json.dumps([None, 7, "text"]).encode(), source_name="demo", is_demo=True
    )
    assert source.read(max_records=3) == [None, 7, "text"]


def test_saved_html_reads_json_ld_without_network() -> None:
    content = b"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Nike Air Max 90",
        "sku": "123456789",
        "brand": {"@type": "Brand", "name": "Nike"},
        "size": "EU 42",
        "itemCondition": "https://schema.org/UsedCondition",
        "datePosted": "2026-10-18T20:00:00+02:00",
        "offers": {
          "@type": "Offer",
          "price": "250.00",
          "priceCurrency": "PLN",
          "url": "https://www.vinted.pl/items/123456789-nike-air-max"
        }
      }
      </script>
    </head></html>
    """
    records = SavedHTMLOfferSource(content, source_name="vinted-manual", is_demo=False).read(
        max_records=10
    )
    assert records == [
        {
            "external_id": "123456789",
            "product_name": "Nike Air Max 90",
            "brand": "Nike",
            "size": "EU 42",
            "condition": "unknown",
            "currency": "PLN",
            "asking_price": "250.00",
            "published_at": "2026-10-18T20:00:00+02:00",
        }
    ]


def test_saved_html_uses_open_graph_fallback() -> None:
    content = b"""
    <html><head>
      <meta property="og:title" content="Adidas Campus 00s">
      <meta property="og:url" content="https://www.vinted.pl/items/987654321-adidas-campus">
      <meta property="product:price:amount" content="199.99">
      <meta property="product:price:currency" content="PLN">
      <meta property="product:brand" content="Adidas">
    </head></html>
    """
    records = SavedHTMLOfferSource(content, source_name="vinted-manual", is_demo=False).read(
        max_records=1
    )
    assert records == [
        {
            "external_id": "987654321",
            "product_name": "Adidas Campus 00s",
            "brand": "Adidas",
            "currency": "PLN",
            "asking_price": "199.99",
        }
    ]


@pytest.mark.parametrize(
    "content",
    [
        b"<html><head></head></html>",
        b'<script type="application/ld+json">{</script>',
    ],
)
def test_saved_html_rejects_missing_or_invalid_metadata(content: bytes) -> None:
    with pytest.raises(SourceError):
        SavedHTMLOfferSource(content, source_name="manual", is_demo=False).read(max_records=10)


def test_saved_html_enforces_record_limit() -> None:
    product = {
        "@type": "Product",
        "name": "Test",
        "offers": {"@type": "Offer", "price": "10", "priceCurrency": "PLN"},
    }
    first = {**product, "sku": "1"}
    second = {**product, "sku": "2"}
    content = (
        '<script type="application/ld+json">' + json.dumps([first, second]) + "</script>"
    ).encode()
    with pytest.raises(SourceError, match="record limit"):
        SavedHTMLOfferSource(content, source_name="manual", is_demo=False).read(max_records=1)
