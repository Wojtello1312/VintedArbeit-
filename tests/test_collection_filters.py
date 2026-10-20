"""All pages below are synthetic HTML; no real network requests."""

from decimal import Decimal

import pytest

from tests.test_vinted_playwright import ITEM_123, item_html
from vinted_deal_hunter.collection_filters import CollectionFilters
from vinted_deal_hunter.models import OfferInput
from vinted_deal_hunter.vinted import parse_offer_html
from vinted_deal_hunter.vinted_playwright import VintedAutomationConfig, configured_vinted_source


def sample(**changes):
    return OfferInput.model_validate(
        {
            "external_id": "123",
            "source_name": "test",
            "is_demo": True,
            "product_name": "DEMO",
            "currency": "PLN",
            "asking_price": "100.00",
            "condition": "very_good",
            "category": "Kobiety > Buty > Sneakersy",
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, None),
        ({"asking_price": "100.01"}, "price_above_maximum"),
        ({"asking_price": "9.99"}, "price_below_minimum"),
        ({"currency": "DKK"}, "currency_mismatch"),
        ({"condition": "good"}, "condition_unknown_or_mismatch"),
        ({"condition": "unknown"}, "condition_unknown_or_mismatch"),
        ({"condition": None}, "condition_unknown_or_mismatch"),
        ({"category": None}, "category_unknown_or_mismatch"),
        ({"category": "Akcesoria do butów"}, "category_unknown_or_mismatch"),
    ],
)
def test_filter_boundaries_and_unknown_metadata(changes, expected):
    criteria = CollectionFilters(
        min_price=Decimal("10"), max_price=Decimal("100"), condition="very_good", category="Buty"
    )
    assert criteria.rejection(sample(**changes)) == expected


def test_rendered_attributes_supply_condition_size_brand_and_category():
    html = (
        item_html("123")
        + b"""<section>
    <div data-testid="item-attributes-status"><span>Stan</span><span>Bardzo dobry</span></div>
    <div data-testid="item-attributes-size">Rozmiar <span>42</span></div>
    <div data-testid="item-attributes-brand">Marka <a>Nike</a></div>
    <div data-testid="item-attributes-category">Kategoria <span>Buty</span></div>
    </section>"""
    )
    parsed = parse_offer_html(html, ITEM_123)
    assert (parsed.condition, parsed.size, parsed.brand, parsed.category) == (
        "very_good",
        "42",
        "nike",
        "Buty",
    )
    assert parsed.listing_url == "https://www.vinted.pl/items/123"
    unknown = parse_offer_html(html.replace(b"Bardzo dobry", b"Perfectly perfect"), ITEM_123)
    assert unknown.condition == "unknown"


def test_link_cannot_be_constructed_from_malicious_identifier():
    assert sample(source_name="vinted-public-pl", is_demo=False).listing_url
    assert (
        sample(
            source_name="vinted-public-pl", is_demo=False, external_id='123" onclick=bad'
        ).listing_url
        is None
    )
    assert sample(source_name="vinted-public-pl").listing_url is None


def test_category_from_explicit_breadcrumbs_and_colon_labels():
    html = (
        item_html("123")
        + b"""<nav data-testid="breadcrumbs">
      <a>Kobiety</a><a>Buty</a><a>Sneakersy</a></nav>
      <div data-testid="item-attributes-size"><span>Rozmiar:</span><span>42</span></div>"""
    )
    parsed = parse_offer_html(html, ITEM_123)
    assert parsed.category == "Kobiety > Buty > Sneakersy"
    assert parsed.size == "42"
    assert CollectionFilters(category="Buty").rejection(parsed) is None


def test_filtered_catalog_preserves_multiple_categories():
    source = configured_vinted_source(
        VintedAutomationConfig(
            enabled=True,
            urls=("https://www.vinted.pl/catalog?catalog[]=1&catalog[]=2&price_to=500",),
            filters=CollectionFilters(max_price=Decimal("100")),
        )
    )
    assert "catalog%5B%5D=1&catalog%5B%5D=2" in source.urls[0]
    assert "price_to=100" in source.urls[0] and "price_to=500" not in source.urls[0]
