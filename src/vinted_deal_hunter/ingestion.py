"""Import use case shared by the CLI and REST endpoint."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import Disposition, OfferRepository
from vinted_deal_hunter.models import OfferInput, SourceIdentity
from vinted_deal_hunter.sources import OfferSource, SourceError, StreamingOfferSource

logger = logging.getLogger(__name__)


class RecordError(BaseModel):
    record_number: int
    field: str
    code: str
    message: str


class ImportReport(BaseModel):
    import_id: str
    source_name: str
    is_demo: bool
    started_at: datetime
    finished_at: datetime
    total: int = 0
    new: int = 0
    skipped: int = 0
    errors: int = 0
    skip_reasons: dict[str, int] = Field(default_factory=dict)
    record_errors: list[RecordError] = Field(default_factory=list)
    diagnostics_truncated: bool = False
    completed: bool = True


class PartialImportError(SourceError):
    """A source stopped after some records; completed commits remain durable."""

    def __init__(self, report: ImportReport, cause: Exception) -> None:
        super().__init__(
            "Source stopped; previously imported records have been preserved",
            code=cause.code if isinstance(cause, SourceError) else None,
        )
        self.report = report
        self.original_error_type = type(cause).__name__


def import_offers(
    source: OfferSource,
    repository: OfferRepository,
    settings: Settings,
    *,
    progress: Callable[[ImportReport], None] | None = None,
) -> ImportReport:
    started = datetime.now(UTC)
    # Validate source identity even for an empty import.
    try:
        identity = SourceIdentity(source_name=source.source_name, is_demo=source.is_demo)
    except ValidationError as exc:
        raise SourceError("Invalid source identity or demo flag") from exc
    report = ImportReport(
        import_id=str(uuid4()),
        source_name=identity.source_name,
        is_demo=identity.is_demo,
        started_at=started,
        finished_at=started,
    )
    valid: list[OfferInput] = []
    numbers: list[int] = []

    def record_error(number: int, field: str, code: str, message: str) -> None:
        report.errors += 1
        if len(report.record_errors) < settings.max_report_errors:
            report.record_errors.append(
                RecordError(
                    record_number=number,
                    field=field,
                    code=code,
                    message=message,
                )
            )
        else:
            report.diagnostics_truncated = True

    def consume(raw: object) -> None:
        report.total += 1
        number = report.total
        if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
            record_error(number, "record", "invalid_record", "Expected an object with offer fields")
            return
        data = dict(raw)
        # Provenance belongs to the adapter/import context, not to untrusted rows.
        if "source_name" in data or "is_demo" in data:
            record_error(
                number, "record", "reserved_metadata", "Provenance fields belong to the source"
            )
            return
        try:
            offer = OfferInput.model_validate(
                {
                    **data,
                    "source_name": identity.source_name,
                    "is_demo": identity.is_demo,
                }
            )
        except ValidationError as exc:
            first = exc.errors(include_input=False, include_context=False, include_url=False)[0]
            record_error(
                number, ".".join(map(str, first["loc"])) or "record", first["type"], first["msg"]
            )
            return
        valid.append(offer)
        numbers.append(number)

    def persist() -> None:
        outcomes = repository.add_many(valid)
        for number, outcome in zip(numbers, outcomes, strict=True):
            if outcome == Disposition.NEW:
                report.new += 1
            elif outcome == Disposition.PROVENANCE_CONFLICT:
                record_error(
                    number, "is_demo", outcome.value, "Stored identity has different provenance"
                )
            else:
                report.skipped += 1
                report.skip_reasons[outcome.value] = report.skip_reasons.get(outcome.value, 0) + 1
        valid.clear()
        numbers.clear()

    def checkpoint() -> None:
        report.finished_at = datetime.now(UTC)
        if progress is not None:
            progress(report)

    if isinstance(source, StreamingOfferSource):
        records = source.iter_records(max_records=settings.max_import_records)
        try:
            while True:
                try:
                    raw = next(records)
                    if report.total >= settings.max_import_records:
                        raise SourceError("Import exceeds the record limit")
                except StopIteration:
                    break
                except Exception as exc:
                    if report.total == 0:
                        raise
                    report.completed = False
                    checkpoint()
                    raise PartialImportError(report, exc) from exc
                consume(raw)
                persist()  # Commit before asking the source for the next page.
                checkpoint()
        finally:
            close = getattr(records, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.warning("Streaming source cleanup failed")
    else:
        # Local documents remain atomic: validate structure and limits before any write.
        batch = source.read(max_records=settings.max_import_records)
        if len(batch) > settings.max_import_records:
            raise SourceError("Import exceeds the record limit")
        for raw in batch:
            consume(raw)
        persist()
    checkpoint()
    logger.info(
        "Import completed id=%s source=%s new=%d skipped=%d errors=%d",
        report.import_id,
        report.source_name,
        report.new,
        report.skipped,
        report.errors,
    )
    return report
