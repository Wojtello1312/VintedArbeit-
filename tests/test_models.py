from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from vinted_deal_hunter.models import Condition, OfferInput


def normalize(raw: dict[str, object]) -> OfferInput:
    return OfferInput.model_validate({**raw, "source_name": " DEMO ", "is_demo": True})


def test_normalization_and_exact_money(raw_offer: dict[str, object]) -> None:
    offer = normalize(raw_offer)
    assert offer.source_name == "demo"
    assert offer.external_id == "001"
    assert offer.brand == "nike"
    assert offer.product_name == "Demo shoes"
    assert offer.size == "M"
    assert offer.condition == Condition.VERY_GOOD
    assert offer.currency == "PLN"
    assert offer.asking_price == Decimal("12.30")
    assert offer.price_minor == 1230
    assert offer.published_at == datetime(2026, 1, 2, 10, 30, tzinfo=UTC)
    assert '"asking_price":"12.30"' in offer.model_dump_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asking_price", "0"),
        ("asking_price", "-1"),
        ("asking_price", "NaN"),
        ("asking_price", Decimal("Infinity")),
        ("asking_price", True),
        ("asking_price", 1.2),
        ("asking_price", "1,234.56"),
        ("asking_price", "12.345"),
        ("asking_price", "1000000001"),
        ("currency", "XYZ"),
        ("currency", "€"),
        ("currency", ""),
        ("condition", "like new maybe"),
        ("external_id", " "),
        ("external_id", True),
        ("external_id", 1.5),
        ("external_id", "a\x00b"),
        ("product_name", " "),
        ("published_at", "2026-01-01T00:00:00"),
        ("published_at", 12345),
        ("published_at", "yesterday"),
    ],
)
def test_invalid_values_are_not_defaulted(
    raw_offer: dict[str, object], field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        normalize({**raw_offer, field: value})


def test_optional_unknowns_are_explicit(raw_offer: dict[str, object]) -> None:
    offer = normalize({**raw_offer, "brand": "", "size": "", "condition": "", "published_at": ""})
    assert (offer.brand, offer.size, offer.condition, offer.published_at) == (
        None,
        None,
        None,
        None,
    )
    assert normalize({**raw_offer, "condition": "unknown"}).condition == Condition.UNKNOWN
    # Unknown brand and size labels are retained rather than mapped to known brands/sizes.
    unknown = normalize({**raw_offer, "brand": "Mystery brand", "size": "unlisted"})
    assert unknown.brand == "mystery brand"
    assert unknown.size == "UNLISTED"


def test_identifiers_are_opaque(raw_offer: dict[str, object]) -> None:
    assert normalize({**raw_offer, "external_id": 123}).external_id == "123"
    assert normalize({**raw_offer, "external_id": "Ab 01"}).external_id == "Ab 01"


def test_currency_specific_scale(raw_offer: dict[str, object]) -> None:
    assert normalize({**raw_offer, "currency": "JPY", "asking_price": "123"}).price_minor == 123
    with pytest.raises(ValidationError):
        normalize({**raw_offer, "currency": "JPY", "asking_price": "1.25"})
