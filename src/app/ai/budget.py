"""Durable cache and atomic USD reservations shared by all workers using one SQLite file."""

import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from app.ai.config import AIConfig
from app.ai.schemas import StoredReview, TokenUsage
from app.schemas.pricing import utc_timestamp
from app.storage.migrations import ensure_database

logger = logging.getLogger(__name__)
NANO = Decimal("1000000000")
MILLION = Decimal("1000000")


class BudgetUnavailable(RuntimeError):
    pass


def nanos(amount: Decimal) -> int:
    return int((amount * NANO).to_integral_value(rounding=ROUND_CEILING))


def usage_cost(usage: TokenUsage, config: AIConfig) -> Decimal:
    assert config.input_usd_per_million is not None
    assert config.output_usd_per_million is not None
    cached_rate = config.cached_input_usd_per_million
    if cached_rate is None:
        cached_rate = config.input_usd_per_million  # Conservative: no assumed cache discount.
    cost = (
        (usage.input_tokens - usage.cached_input_tokens) * config.input_usd_per_million
        + usage.cached_input_tokens * cached_rate
        + usage.output_tokens * config.output_usd_per_million
    ) / MILLION
    return Decimal(nanos(cost)) / NANO


@dataclass(frozen=True)
class Claim:
    status: str
    attempt_id: str | None = None
    review: StoredReview | None = None


@dataclass(frozen=True)
class DailyUsage:
    analyses: int
    attempts: int
    accounted_usd: Decimal
    unknown_attempts: int


class AIBudget:
    def __init__(self, config: AIConfig) -> None:
        self.config = config
        try:
            ensure_database(config.db_path, "ai")
        except Exception as exc:
            raise BudgetUnavailable("AI accounting unavailable") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            db = sqlite3.connect(self.config.db_path, timeout=10)
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise BudgetUnavailable("AI accounting unavailable") from exc

    @property
    def reservation_usd(self) -> Decimal:
        return usage_cost(
            TokenUsage(
                input_tokens=self.config.max_input_tokens,
                output_tokens=self.config.max_output_tokens,
                cached_input_tokens=0,
            ),
            self.config,
        )

    def get_cached(self, key: str) -> StoredReview | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT result_json FROM ai_reviews WHERE input_hash=? AND status='complete'",
                (key,),
            ).fetchone()
        return None if row is None else StoredReview.model_validate_json(row[0])

    def _reserve(self, db: sqlite3.Connection, key: str, now: datetime) -> str | None:
        day = utc_timestamp(now).date().isoformat()
        count, spent = db.execute(
            "SELECT COUNT(DISTINCT input_hash), COALESCE(SUM(accounted_nanos),0) "
            "FROM ai_attempts WHERE day=?",
            (day,),
        ).fetchone()
        already_today = db.execute(
            "SELECT 1 FROM ai_attempts WHERE day=? AND input_hash=?", (day, key)
        ).fetchone()
        reserve = nanos(self.reservation_usd)
        # Round the allowed budget DOWN; a fraction of a nanodollar cannot add spending room.
        limit = int(self.config.daily_budget_usd * NANO)
        if (
            not already_today and count >= self.config.max_daily_analyses
        ) or spent + reserve > limit:
            return None
        attempt_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO ai_attempts "
            "(attempt_id,input_hash,day,reserved_nanos,accounted_nanos) VALUES (?,?,?,?,?)",
            (attempt_id, key, day, reserve, reserve),
        )
        return attempt_id

    def claim(self, key: str, model: str, prompt_version: str, now: datetime) -> Claim:
        now = utc_timestamp(now)
        with self._transaction() as db:
            row = db.execute(
                "SELECT status,result_json,created_at FROM ai_reviews WHERE input_hash=?", (key,)
            ).fetchone()
            if row is not None:
                if row[0] == "complete":
                    return Claim("cached", review=StoredReview.model_validate_json(row[1]))
                if row[0] == "running":
                    lease = (
                        self.config.max_attempts
                        * (self.config.timeout_seconds + self.config.max_backoff_seconds)
                        + 120
                    )
                    if (now - datetime.fromisoformat(row[2])).total_seconds() <= lease:
                        return Claim("pending")
                    # Never resend a possibly charged request after a worker crash.
                    db.execute(
                        "UPDATE ai_reviews SET status='uncertain' WHERE input_hash=?", (key,)
                    )
                return Claim("previous_attempt_unavailable")
            db.execute(
                "INSERT INTO ai_reviews VALUES (?,?,?,?,?,NULL)",
                (key, model, prompt_version, now.isoformat(), "running"),
            )
            attempt_id = self._reserve(db, key, now)
            if attempt_id is None:
                db.execute("DELETE FROM ai_reviews WHERE input_hash=?", (key,))
                return Claim("budget_exhausted")
            return Claim("reserved", attempt_id=attempt_id)

    def reserve_retry(self, key: str, now: datetime) -> str | None:
        with self._transaction() as db:
            row = db.execute("SELECT status FROM ai_reviews WHERE input_hash=?", (key,)).fetchone()
            if row is None or row[0] != "running":
                return None
            return self._reserve(db, key, now)

    def settle(self, attempt_id: str, usage: TokenUsage | None) -> Decimal | None:
        """Missing usage (including timeouts) never frees a reservation."""
        cost = None if usage is None else usage_cost(usage, self.config)
        if usage is not None and cost is not None:
            with self._transaction() as db:
                db.execute(
                    "UPDATE ai_attempts SET accounted_nanos=?,input_tokens=?,output_tokens=?,"
                    "cached_input_tokens=?,usage_known=1 WHERE attempt_id=? AND usage_known=0",
                    (
                        nanos(cost),
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cached_input_tokens,
                        attempt_id,
                    ),
                )
        logger.info(
            "AI usage attempt=%s input_tokens=%s output_tokens=%s cached_tokens=%s "
            "cost_usd=%s reserved_usd=%s",
            attempt_id,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            usage.cached_input_tokens if usage else None,
            cost,
            self.reservation_usd,
        )
        return cost

    def finish(self, key: str, review: StoredReview | None) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE ai_reviews SET status=?,result_json=? "
                "WHERE input_hash=? AND status='running'",
                (
                    "complete" if review else "failed",
                    review.model_dump_json() if review else None,
                    key,
                ),
            )

    def daily_usage(self, now: datetime) -> DailyUsage:
        day = utc_timestamp(now).date().isoformat()
        with self._transaction() as db:
            count, attempts, spent, unknown = db.execute(
                "SELECT COUNT(DISTINCT input_hash),COUNT(*),COALESCE(SUM(accounted_nanos),0),"
                "COALESCE(SUM(1-usage_known),0) FROM ai_attempts WHERE day=?",
                (day,),
            ).fetchone()
        return DailyUsage(count, attempts, Decimal(spent) / NANO, unknown)
