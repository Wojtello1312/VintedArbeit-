import json
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.models import OfferInput
from vinted_deal_hunter.sources import CSVOfferSource, JSONOfferSource, MockOfferSource, SourceError


@pytest.mark.parametrize("extension", ["csv", "json", "mock"])
def test_demo_twice_and_reopen(settings: Settings, extension: str) -> None:
    if extension == "mock":
        source = MockOfferSource()
    else:
        content = files("vinted_deal_hunter").joinpath(f"data/demo_offers.{extension}").read_bytes()
        adapter = CSVOfferSource if extension == "csv" else JSONOfferSource
        source = adapter(content, source_name="demo", is_demo=True)
    repo = SQLiteOfferRepository(settings.db_path)
    try:
        first = import_offers(source, repo, settings)
        assert (first.total, first.new, first.skipped, first.errors) == (130, 110, 10, 10)
        assert first.total == first.new + first.skipped + first.errors
        assert first.finished_at >= first.started_at
        assert len(first.record_errors) == 10
        assert first.record_errors[0].record_number == 121
        assert first.skip_reasons == {"duplicate": 10}
        assert all(offer.is_demo for offer in repo.list_offers())
    finally:
        repo.close()
    repo = SQLiteOfferRepository(settings.db_path)
    try:
        second = import_offers(source, repo, settings)
        assert (second.new, second.skipped, second.errors) == (0, 120, 10)
        assert len(repo.list_offers()) == 110
    finally:
        repo.close()


def test_namespaces_changes_and_demo_conflict(
    repository: SQLiteOfferRepository, settings: Settings, raw_offer: dict[str, object]
) -> None:
    def run(data: dict[str, object], source: str = "demo", demo: bool = True):
        return import_offers(
            JSONOfferSource(json.dumps([data]).encode(), source_name=source, is_demo=demo),
            repository,
            settings,
        )

    assert run(raw_offer).new == 1
    assert run(raw_offer, "other").new == 1
    changed = run({**raw_offer, "asking_price": "20"})
    assert changed.skip_reasons == {"changed_existing": 1}
    assert run(raw_offer, demo=False).errors == 1
    assert len(repository.list_offers()) == 2
    assert str(repository.list_offers()[0].asking_price) == "12.30"


def test_record_errors_continue_and_diagnostics_are_bounded(
    repository: SQLiteOfferRepository, settings: Settings, raw_offer: dict[str, object]
) -> None:
    records = [
        None,
        {**raw_offer, "source_name": "spoof"},
        {**raw_offer, "asking_price": "bad"},
        raw_offer,
        {**raw_offer, "external_id": "next"},
    ]
    source = JSONOfferSource(json.dumps(records).encode(), source_name="demo", is_demo=True)
    report = import_offers(source, repository, settings.model_copy(update={"max_report_errors": 1}))
    assert (report.new, report.errors, report.skipped) == (2, 3, 0)
    assert report.diagnostics_truncated
    assert len(report.record_errors) == 1


def test_empty_and_bad_identity(repository: SQLiteOfferRepository, settings: Settings) -> None:
    source = JSONOfferSource(b"[]", source_name="demo", is_demo=True)
    assert import_offers(source, repository, settings).total == 0
    source.source_name = "invalid/source"
    with pytest.raises(SourceError):
        import_offers(source, repository, settings)


def test_corrupt_document_does_not_partially_commit(
    repository: SQLiteOfferRepository, settings: Settings
) -> None:
    content = b'external_id,product_name,currency,asking_price\na,Demo,PLN,1\nb,"unterminated'
    with pytest.raises(SourceError):
        import_offers(
            CSVOfferSource(content, source_name="demo", is_demo=True), repository, settings
        )
    assert repository.list_offers() == []


def test_database_failure_rolls_back_batch(
    repository: SQLiteOfferRepository, raw_offer: dict[str, object]
) -> None:
    def fail_second_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT") and parameters[1] == "failure":
            raise OperationalError("injected failure", {}, None)

    event.listen(repository.engine, "before_cursor_execute", fail_second_insert)
    offers = [
        OfferInput.model_validate(
            {**raw_offer, "external_id": identity, "source_name": "demo", "is_demo": True}
        )
        for identity in ("ok", "failure")
    ]
    try:
        with pytest.raises(OperationalError):
            repository.add_many(offers)
    finally:
        event.remove(repository.engine, "before_cursor_execute", fail_second_insert)
    assert repository.list_offers() == []


def test_concurrent_imports_do_not_duplicate(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "concurrent.sqlite3")
    stores = [SQLiteOfferRepository(settings.db_path) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(
                pool.map(lambda repo: import_offers(MockOfferSource(), repo, settings), stores)
            )
        assert sum(report.new for report in reports) == 110
        assert len(stores[0].list_offers()) == 110
    finally:
        for store in stores:
            store.close()
