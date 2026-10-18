"""Single-host process lock and durable job reports/pause state."""

from datetime import datetime
from pathlib import Path
from typing import TextIO

import portalocker
from pydantic import Field

from app.schemas.pricing import FrozenSchema
from app.storage.operations import OperationsStore, timestamp


class CycleReport(FrozenSchema):
    run_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float = 0
    source_name: str
    is_demo: bool
    fetched: int = 0
    imported: int = 0
    skipped: int = 0
    import_errors: int = 0
    processed: int = 0
    analysis_errors: int = 0
    states: dict[str, int] = Field(default_factory=dict)
    alerts: dict[str, int] = Field(default_factory=dict)
    error_type: str | None = None


class JobStore(OperationsStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.lock_path = path.with_suffix(path.suffix + ".cycle.lock")

    def acquire(self) -> TextIO | None:
        lock = self.lock_path.open("a")
        try:
            portalocker.lock(lock, portalocker.LOCK_EX | portalocker.LOCK_NB)
            return lock
        except portalocker.exceptions.LockException:
            lock.close()
            return None

    def is_running(self) -> bool:
        lease = self.acquire()
        if lease is None:
            return True
        lease.close()
        return False

    def paused(self) -> bool:
        with self.transaction() as db:
            return bool(db.execute("SELECT paused FROM job_control WHERE id=1").fetchone()[0])

    def set_paused(self, paused: bool) -> None:
        with self.transaction() as db:
            db.execute("UPDATE job_control SET paused=? WHERE id=1", (int(paused),))

    def get(self, run_id: str) -> CycleReport | None:
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM job_runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else CycleReport.model_validate_json(row[0])

    def save(self, report: CycleReport) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO job_runs VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE "
                "SET status=excluded.status,payload=excluded.payload",
                (
                    report.run_id,
                    report.status,
                    timestamp(report.started_at),
                    report.model_dump_json(),
                ),
            )

    def recover_interrupted(self, now: datetime) -> None:
        # Caller must hold the OS lock, proving no old cycle can still be running.
        with self.transaction() as db:
            rows = db.execute("SELECT payload FROM job_runs WHERE status='running'").fetchall()
            for row in rows:
                old = CycleReport.model_validate_json(row[0])
                report = old.model_copy(
                    update={
                        "status": "interrupted",
                        "finished_at": now,
                        "error_type": "InterruptedCycle",
                    }
                )
                db.execute(
                    "UPDATE job_runs SET status='interrupted',payload=? WHERE run_id=?",
                    (report.model_dump_json(), report.run_id),
                )

    def latest(self) -> CycleReport | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload FROM job_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else CycleReport.model_validate_json(row[0])

    def totals(self) -> dict[str, int]:
        with self.transaction() as db:
            rows = db.execute("SELECT payload FROM job_runs").fetchall()
        reports = [CycleReport.model_validate_json(row[0]) for row in rows]
        return {
            "cycles": len(reports),
            "processed": sum(row.processed for row in reports),
            "failed_cycles": sum(row.status in {"error", "interrupted"} for row in reports),
        }
