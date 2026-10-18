from threading import RLock

from app.schemas.deals import AnalysisRecord


class MemoryAnalysisStore:
    """Thread-safe ephemeral adapter for offline backtests and tests."""

    def __init__(self) -> None:
        self._records: dict[str, AnalysisRecord] = {}
        self._lock = RLock()

    def create(self, record: AnalysisRecord) -> AnalysisRecord:
        with self._lock:
            return self._records.setdefault(record.analysis_id, record)

    def get(self, analysis_id: str) -> AnalysisRecord | None:
        with self._lock:
            return self._records.get(analysis_id)

    def compare_and_set(self, record: AnalysisRecord, expected_revision: int) -> bool:
        if record.revision != expected_revision + 1:
            raise ValueError("a commit must advance revision by exactly one")
        with self._lock:
            previous = self._records.get(record.analysis_id)
            if previous is None or previous.revision != expected_revision:
                return False
            self._records[record.analysis_id] = record
            return True
