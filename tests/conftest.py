from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository


@pytest.fixture
def raw_offer() -> dict[str, object]:
    return {
        "external_id": " 001 ",
        "product_name": "  Demo   shoes ",
        "brand": " NIKE ",
        "size": " m ",
        "condition": "bardzo dobry",
        "currency": "pln",
        "asking_price": "12,30",
        "published_at": "2026-01-02T12:30:00+02:00",
    }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "offers.sqlite3",
        jobs_api_token=SecretStr("test-only-api-token-" + "x" * 32),
    )


@pytest.fixture
def repository(settings: Settings) -> Iterator[SQLiteOfferRepository]:
    store = SQLiteOfferRepository(settings.db_path)
    yield store
    store.close()
