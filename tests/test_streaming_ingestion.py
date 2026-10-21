"""Streaming sources commit independent records; local documents retain batch semantics."""

from unittest.mock import Mock

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import PartialImportError, import_offers
from vinted_deal_hunter.sources import SourceError, SourceErrorCode


class SyntheticStream:
    source_name = "synthetic-stream"
    is_demo = True

    def __init__(self, records, failure=None):
        self.records = records
        self.failure = failure
        self.closed = False

    def read(self, *, max_records):
        raise AssertionError("Streaming ingestion must not buffer the entire source")

    def iter_records(self, *, max_records):
        try:
            yield from self.records
            if self.failure is not None:
                raise self.failure
        finally:
            self.closed = True


def test_commits_are_visible_before_next_record_and_survive_interruption(settings, raw_offer):
    repository = SQLiteOfferRepository(settings.db_path)

    def records():
        for i in range(60):
            yield {**raw_offer, "external_id": str(i)}
            # Independent connections observe the commit before the source continues.
            observer = SQLiteOfferRepository(settings.db_path)
            try:
                assert len(observer.list_offers()) == i + 1
            finally:
                observer.close()

    source = SyntheticStream(records(), KeyboardInterrupt())
    checkpoints = []
    try:
        with pytest.raises(KeyboardInterrupt):
            import_offers(
                source, repository, settings, progress=lambda r: checkpoints.append(r.new)
            )
    finally:
        repository.close()
    assert checkpoints[-1] == 60 and source.closed
    reopened = SQLiteOfferRepository(settings.db_path)
    try:
        assert len(reopened.list_offers()) == 60
        resumed = import_offers(
            SyntheticStream([{**raw_offer, "external_id": str(i)} for i in range(100)]),
            reopened,
            settings,
        )
        assert (resumed.total, resumed.new, resumed.skipped) == (100, 40, 60)
        assert resumed.completed
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "failure", [SourceError("private source details"), RuntimeError("private")]
)
def test_partial_report_keeps_validation_counts_and_deduplication(
    failure, settings, repository, raw_offer
):
    source = SyntheticStream([None, raw_offer, raw_offer], failure)
    with pytest.raises(PartialImportError) as caught:
        import_offers(source, repository, settings)
    report = caught.value.report
    assert not report.completed
    assert (report.total, report.new, report.skipped, report.errors) == (3, 1, 1, 1)
    assert report.finished_at >= report.started_at
    assert "private" not in str(caught.value)
    assert source.closed and len(repository.list_offers()) == 1


def test_stream_failure_before_first_record_keeps_original_diagnosis(settings, repository):
    error = SourceError("missing", code=SourceErrorCode.PLAYWRIGHT_NOT_INSTALLED)
    with pytest.raises(SourceError) as caught:
        import_offers(SyntheticStream([], error), repository, settings)
    assert caught.value is error
    assert repository.list_offers() == []


def test_stream_cannot_exceed_record_budget(settings, repository, raw_offer):
    source = SyntheticStream([{**raw_offer, "external_id": str(i)} for i in range(5)])
    with pytest.raises(PartialImportError) as caught:
        import_offers(source, repository, settings.model_copy(update={"max_import_records": 2}))
    assert caught.value.report.total == caught.value.report.new == 2
    assert len(repository.list_offers()) == 2 and source.closed


def test_storage_failure_stops_stream_preserves_prior_commits_and_closes_source(
    settings, repository, raw_offer
):
    def fail_second_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT") and parameters[1] == "failure":
            raise OperationalError("injected failure", {}, None)

    event.listen(repository.engine, "before_cursor_execute", fail_second_insert)
    source = SyntheticStream(
        [{**raw_offer, "external_id": identity} for identity in ("ok", "failure", "not-read")]
    )
    checkpoint = Mock()
    try:
        with pytest.raises(OperationalError):
            import_offers(source, repository, settings, progress=checkpoint)
    finally:
        event.remove(repository.engine, "before_cursor_execute", fail_second_insert)
    assert source.closed
    assert [offer.external_id for offer in repository.list_offers()] == ["ok"]
    assert checkpoint.call_count == 1
