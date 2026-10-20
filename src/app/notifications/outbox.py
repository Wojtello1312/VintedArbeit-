"""At-most-one automatic delivery per listing/recipient; uncertain attempts stay reserved."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.notifications.config import NotificationConfig
from app.notifications.formatting import AlertMessage
from app.notifications.telegram import DeliveryResult
from app.schemas.deals import NotificationReceipt
from app.storage.operations import OperationsStore, timestamp


@dataclass(frozen=True)
class ClaimedAlert:
    message: AlertMessage
    attempt_id: str


@dataclass(frozen=True)
class SentAlert:
    message: AlertMessage
    receipt: NotificationReceipt


class AlertOutbox(OperationsStore):
    def __init__(self, path: Path, config: NotificationConfig) -> None:
        super().__init__(path)
        self.config = config

    def cancel_offer(self, source: str, external: str) -> None:
        """Caller holds the cycle lease; never change sent or uncertain deliveries."""
        with self.transaction() as db:
            db.execute(
                "UPDATE alert_outbox SET status='cancelled',reason='offer_deleted' "
                "WHERE status IN ('pending','retry') "
                "AND json_extract(payload,'$.source_name')=? "
                "AND json_extract(payload,'$.external_id')=?",
                (source, external),
            )

    def cancel_demo(self) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE alert_outbox SET status='cancelled',reason='offer_deleted' "
                "WHERE is_demo=1 AND status IN ('pending','retry')"
            )

    def enqueue(self, message: AlertMessage, now: datetime) -> bool:
        with self.transaction() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO alert_outbox "
                "(alert_key,destination,is_demo,payload,status,created_at,due_at) "
                "VALUES (?,?,?,?,'pending',?,?)",
                (
                    message.alert_key,
                    message.destination,
                    int(message.is_demo),
                    message.model_dump_json(),
                    timestamp(now),
                    timestamp(now),
                ),
            )
            return result.rowcount == 1

    def recover(self, now: datetime) -> None:
        cutoff = timestamp(now - timedelta(seconds=self.config.sending_lease_seconds))
        with self.transaction() as db:
            db.execute(
                "UPDATE notification_slots SET status='uncertain' WHERE attempt_id IN "
                "(SELECT attempt_id FROM alert_outbox WHERE status='sending' AND claimed_at<?)",
                (cutoff,),
            )
            db.execute(
                "UPDATE alert_outbox SET status='uncertain',reason='interrupted_delivery' "
                "WHERE status='sending' AND claimed_at<?",
                (cutoff,),
            )

    def pending(
        self, now: datetime, destination: str | None = None, allow_production: bool = True
    ) -> list[AlertMessage]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT payload FROM alert_outbox WHERE status IN ('pending','retry') "
                "AND due_at<=? AND (is_demo=1 OR (? AND (? IS NULL OR destination=?))) "
                "ORDER BY is_demo DESC,created_at,alert_key LIMIT ?",
                (
                    timestamp(now),
                    int(allow_production),
                    destination,
                    destination,
                    self.config.batch_size,
                ),
            ).fetchall()
        return [AlertMessage.model_validate_json(row[0]) for row in rows]

    def claim(self, key: str, now: datetime) -> ClaimedAlert | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM alert_outbox WHERE alert_key=?", (key,)).fetchone()
            if (
                row is None
                or row["status"] not in ("pending", "retry")
                or row["due_at"] > timestamp(now)
            ):
                return None
            if row["attempts"] >= self.config.max_attempts:
                db.execute(
                    "UPDATE alert_outbox SET status='failed',reason='attempts_exhausted' "
                    "WHERE alert_key=?",
                    (key,),
                )
                return None
            attempt_id = uuid.uuid4().hex
            if not row["is_demo"]:
                day = timestamp(now)[:10]
                used = db.execute(
                    "SELECT COUNT(*) FROM notification_slots WHERE day=? AND status!='released'",
                    (day,),
                ).fetchone()[0]
                if used >= self.config.daily_limit:
                    db.execute(
                        "UPDATE alert_outbox SET reason='daily_limit' WHERE alert_key=?", (key,)
                    )
                    return None
                db.execute(
                    "INSERT INTO notification_slots VALUES (?,?,?,?, 'reserved')",
                    (attempt_id, key, row["destination"], day),
                )
            db.execute(
                "UPDATE alert_outbox SET status='sending',attempts=attempts+1,"
                "attempt_id=?,claimed_at=? WHERE alert_key=?",
                (attempt_id, timestamp(now), key),
            )
            return ClaimedAlert(AlertMessage.model_validate_json(row["payload"]), attempt_id)

    def complete(self, claim: ClaimedAlert, result: DeliveryResult, now: datetime) -> str | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM alert_outbox WHERE alert_key=?", (claim.message.alert_key,)
            ).fetchone()
            if row is None or row["status"] != "sending" or row["attempt_id"] != claim.attempt_id:
                return None
            status, reason = result.status, result.reason
            due = now
            if status == "sent" and not result.receipt_id:
                status, reason = "uncertain", "missing_receipt"
            if status == "retry":
                delay = max(
                    result.retry_after,
                    min(
                        self.config.max_backoff_seconds,
                        self.config.backoff_seconds * 2 ** (row["attempts"] - 1),
                    ),
                )
                if row["attempts"] >= self.config.max_attempts:
                    status, reason = "failed", "attempts_exhausted"
                elif delay > self.config.max_backoff_seconds:
                    status, reason = "failed", "retry_after_exceeds_limit"
                else:
                    due = now + timedelta(seconds=delay)
            slot_status = (
                "sent"
                if status == "sent"
                else ("uncertain" if status == "uncertain" else "released")
            )
            db.execute(
                "UPDATE notification_slots SET status=? WHERE attempt_id=?",
                (slot_status, claim.attempt_id),
            )
            db.execute(
                "UPDATE alert_outbox SET status=?,reason=?,due_at=?,receipt_id=?,delivered_at=? "
                "WHERE alert_key=?",
                (
                    status,
                    reason,
                    timestamp(due),
                    result.receipt_id if status == "sent" else None,
                    timestamp(now) if status == "sent" else None,
                    claim.message.alert_key,
                ),
            )
            return status

    def unreconciled(self) -> list[SentAlert]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT payload,receipt_id,delivered_at FROM alert_outbox "
                "WHERE status='sent' AND checkpoint_done=0 LIMIT ?",
                (self.config.batch_size,),
            ).fetchall()
        return [
            SentAlert(
                message=AlertMessage.model_validate_json(row[0]),
                receipt=NotificationReceipt(
                    receipt_id=row[1],
                    channel="demo"
                    if AlertMessage.model_validate_json(row[0]).is_demo
                    else "telegram",
                    delivered_at=datetime.fromisoformat(row[2]),
                ),
            )
            for row in rows
        ]

    def reconciled(self, key: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE alert_outbox SET checkpoint_done=1 WHERE alert_key=? AND status='sent'",
                (key,),
            )

    def counts(self) -> dict[str, int]:
        with self.transaction() as db:
            rows = db.execute("SELECT status,COUNT(*) FROM alert_outbox GROUP BY status").fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def daily_count(self, destination: str | None, now: datetime) -> int:
        with self.transaction() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM notification_slots WHERE (? IS NULL OR destination=?) "
                "AND day=? AND status!='released'",
                (destination, destination, timestamp(now)[:10]),
            ).fetchone()[0]
        return int(count)
