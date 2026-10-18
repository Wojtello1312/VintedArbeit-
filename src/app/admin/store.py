"""Durable sessions, throttling, settings and manual feedback, separate from scoring."""

import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from app.admin.policy import PanelPolicy
from app.storage.operations import OperationsStore, timestamp

Feedback = Literal["good_deal", "bad_deal", "purchased", "ignored"]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class RevisionConflict(ValueError):
    pass


class AdminStore(OperationsStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)

    def allow_login(self, bucket: str, now: datetime, limit: int, window: int) -> bool:
        with self.transaction() as db:
            cutoff = timestamp(now - timedelta(seconds=window))
            db.execute("DELETE FROM admin_login_attempts WHERE recorded_at<?", (cutoff,))
            used = db.execute(
                "SELECT COUNT(*) FROM admin_login_attempts WHERE bucket=?", (bucket,)
            ).fetchone()[0]
            if used >= limit:
                return False
            db.execute("INSERT INTO admin_login_attempts VALUES (?,?)", (bucket, timestamp(now)))
            return True

    def new_session(self, now: datetime, ttl: int, fingerprint: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (timestamp(now),))
            db.execute(
                "INSERT INTO admin_sessions VALUES (?,?,?,?)",
                (digest(token), csrf, timestamp(now + timedelta(seconds=ttl)), fingerprint),
            )
        return token, csrf

    def session(self, token: str, now: datetime, fingerprint: str) -> str | None:
        if len(token) > 128:
            return None
        with self.transaction() as db:
            row = db.execute(
                "SELECT csrf FROM admin_sessions WHERE token_hash=? "
                "AND expires_at>? AND fingerprint=?",
                (digest(token), timestamp(now), fingerprint),
            ).fetchone()
        return None if row is None else str(row[0])

    def logout(self, token: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (digest(token),))

    def policy(self) -> tuple[int, PanelPolicy | None]:
        with self.transaction() as db:
            row = db.execute("SELECT revision,payload FROM admin_policy WHERE id=1").fetchone()
        return (0, None) if row is None else (row[0], PanelPolicy.model_validate_json(row[1]))

    def save_policy(self, policy: PanelPolicy, expected_revision: int, now: datetime) -> int:
        with self.transaction() as db:
            row = db.execute("SELECT revision FROM admin_policy WHERE id=1").fetchone()
            current = row[0] if row else 0
            if current != expected_revision:
                raise RevisionConflict("Ustawienia zmieniły się. Odśwież formularz.")
            db.execute(
                "INSERT INTO admin_policy VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE "
                "SET revision=excluded.revision,payload=excluded.payload,"
                "updated_at=excluded.updated_at",
                (current + 1, policy.model_dump_json(), timestamp(now)),
            )
        return int(current + 1)

    def feedback(self, source: str, external: str, label: Feedback, now: datetime) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO admin_feedback VALUES (?,?,?,?) "
                "ON CONFLICT(source_name,external_id) DO UPDATE "
                "SET label=excluded.label,updated_at=excluded.updated_at",
                (source, external, label, timestamp(now)),
            )
            db.execute(
                "INSERT INTO admin_feedback_history "
                "(source_name,external_id,label,recorded_at) VALUES (?,?,?,?)",
                (source, external, label, timestamp(now)),
            )

    def feedback_history(self, source: str, external: str) -> list[dict[str, str]]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT label,recorded_at FROM admin_feedback_history WHERE "
                "source_name=? AND external_id=? ORDER BY id DESC LIMIT 50",
                (source, external),
            ).fetchall()
        return [dict(row) for row in rows]
