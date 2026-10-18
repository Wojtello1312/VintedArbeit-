"""Persistence contract; pure calculation may repeat, committed state transitions may not."""

from typing import Protocol

from app.schemas.deals import AnalysisRecord


class StoreUnavailable(RuntimeError):
    """A checkpoint could not be read or committed. Never report it as saved."""


class AnalysisStore(Protocol):
    def create(self, record: AnalysisRecord) -> AnalysisRecord: ...

    def get(self, analysis_id: str) -> AnalysisRecord | None: ...

    def compare_and_set(self, record: AnalysisRecord, expected_revision: int) -> bool: ...
