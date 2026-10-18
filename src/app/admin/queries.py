"""Read models over existing SQLite facts; no synthetic production values."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from app.scheduler.store import CycleReport
from app.schemas.deals import AnalysisRecord, DealState
from app.schemas.pricing import FrozenSchema
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.models import CURRENCY_SCALE, OfferInput


def offer_key(source: str, external: str) -> str:
    return hashlib.sha256(json.dumps([source, external]).encode()).hexdigest()


class OfferFilters(FrozenSchema):
    q: str = Field(default="", max_length=200)
    brand: str = Field(default="", max_length=200)
    currency: str = "PLN"
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, ge=0)
    min_score: Decimal | None = Field(default=None, ge=0, le=100)
    min_profit: Decimal | None = None
    risk: Literal["", "LOW", "MEDIUM", "HIGH", "unknown"] = ""
    label: Literal["", "good_deal", "bad_deal", "purchased", "ignored"] = ""
    mode: Literal["all", "demo", "production"] = "all"
    sort: Literal["newest", "price_asc", "price_desc", "score", "profit", "name"] = "newest"
    page: int = Field(default=1, ge=1, le=100000)

    @field_validator("min_price", "max_price", "min_score", "min_profit", mode="before")
    @classmethod
    def empty_number(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("currency")
    @classmethod
    def supported_currency(cls, value: str) -> str:
        if value not in CURRENCY_SCALE:
            raise ValueError("unsupported currency")
        return value


@dataclass(frozen=True)
class OfferView:
    key: str
    offer: OfferInput
    analysis: AnalysisRecord | None
    label: str | None


LATEST = """WITH ranked AS (
 SELECT payload,analysis_id,ROW_NUMBER() OVER (
 PARTITION BY json_extract(payload,'$.request.snapshot.offer.source_name'),
              json_extract(payload,'$.request.snapshot.offer.external_id')
 ORDER BY julianday(json_extract(payload,'$.request.as_of')) DESC,
          julianday(json_extract(payload,'$.history[#-1].recorded_at')) DESC, analysis_id DESC
 ) AS position FROM analyses.deal_analyses
), inventory AS (
 SELECT o.source_name,o.external_id,
 coalesce(json_extract(a.payload,'$.request.snapshot.offer'),o.payload) AS offer,
 a.payload AS analysis, f.label AS label
 FROM offers o LEFT JOIN ranked a ON a.position=1 AND
 json_extract(a.payload,'$.request.snapshot.offer.source_name')=o.source_name AND
 json_extract(a.payload,'$.request.snapshot.offer.external_id')=o.external_id
 LEFT JOIN operations.admin_feedback f ON f.source_name=o.source_name
 AND f.external_id=o.external_id
) """


class AdminQueries:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.settings.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.create_function("offer_key", 2, offer_key, deterministic=True)
        db.create_function("fold", 1, lambda value: (value or "").casefold(), deterministic=True)
        db.create_function(
            "decimal_ge", 2, lambda a, b: a is not None and Decimal(str(a)) >= Decimal(b)
        )
        db.create_function(
            "decimal_le", 2, lambda a, b: a is not None and Decimal(str(a)) <= Decimal(b)
        )
        db.create_collation(
            "decimal", lambda a, b: (Decimal(a) > Decimal(b)) - (Decimal(a) < Decimal(b))
        )
        try:
            db.execute(
                "ATTACH DATABASE ? AS analyses",
                (self.settings.db_path.with_name("deals.sqlite3").resolve().as_uri() + "?mode=ro",),
            )
            db.execute(
                "ATTACH DATABASE ? AS operations",
                (self.settings.jobs_db_path.resolve().as_uri() + "?mode=ro",),
            )
            yield db
        finally:
            db.close()

    @staticmethod
    def view(row: sqlite3.Row) -> OfferView:
        offer = OfferInput.model_validate_json(row["offer"])
        analysis = (
            None if row["analysis"] is None else AnalysisRecord.model_validate_json(row["analysis"])
        )
        return OfferView(
            offer_key(offer.source_name, offer.external_id), offer, analysis, row["label"]
        )

    def offers(self, filters: OfferFilters, *, deals: bool = False) -> tuple[list[OfferView], int]:
        conditions = ["json_extract(offer,'$.currency')=?"]
        params: list[object] = [filters.currency]
        if filters.q:
            conditions.append(
                "(instr(fold(json_extract(offer,'$.product_name')),fold(?))>0 "
                "OR instr(fold(external_id),fold(?))>0)"
            )
            params.extend([filters.q, filters.q])
        if filters.brand:
            conditions.append("fold(json_extract(offer,'$.brand'))=fold(?)")
            params.append(filters.brand)
        for value, field, comparison in (
            (filters.min_price, "json_extract(offer,'$.asking_price')", "decimal_ge"),
            (filters.max_price, "json_extract(offer,'$.asking_price')", "decimal_le"),
            (filters.min_score, "json_extract(analysis,'$.score.total')", "decimal_ge"),
            (
                filters.min_profit,
                "json_extract(analysis,'$.profit.estimated_profit')",
                "decimal_ge",
            ),
        ):
            if value is not None:
                conditions.append(f"{comparison}({field},?)")
                params.append(str(value))
        if filters.risk:
            conditions.append("coalesce(json_extract(analysis,'$.risk.level'),'unknown')=?")
            params.append(filters.risk)
        if filters.label:
            conditions.append("label=?")
            params.append(filters.label)
        if filters.mode != "all":
            conditions.append("json_extract(offer,'$.is_demo')=?")
            params.append(int(filters.mode == "demo"))
        if deals:
            conditions.append("json_extract(analysis,'$.state') IN ('APPROVED','NOTIFIED')")
        ordering = {
            "newest": "julianday(json_extract(offer,'$.published_at')) DESC",
            "price_asc": "json_extract(offer,'$.asking_price') COLLATE decimal ASC",
            "price_desc": "json_extract(offer,'$.asking_price') COLLATE decimal DESC",
            "score": "json_extract(analysis,'$.score.total') COLLATE decimal DESC",
            "profit": "json_extract(analysis,'$.profit.estimated_profit') COLLATE decimal DESC",
            "name": "fold(json_extract(offer,'$.product_name')) ASC",
        }[filters.sort]
        where = " WHERE " + " AND ".join(conditions)
        with self.connection() as db:
            total = db.execute(
                LATEST + "SELECT COUNT(*) FROM inventory" + where, params
            ).fetchone()[0]
            rows = db.execute(
                LATEST
                + "SELECT * FROM inventory"
                + where
                + " ORDER BY "
                + ordering
                + ",source_name,external_id LIMIT 25 OFFSET ?",
                (*params, (filters.page - 1) * 25),
            ).fetchall()
        return [self.view(row) for row in rows], int(total)

    def offer(self, key: str) -> OfferView | None:
        with self.connection() as db:
            row = db.execute(
                LATEST + "SELECT * FROM inventory WHERE offer_key(source_name,external_id)=?",
                (key,),
            ).fetchone()
        return None if row is None else self.view(row)

    def history(self, offer: OfferInput, page: int = 1) -> tuple[list[AnalysisRecord], int]:
        query = (
            "FROM analyses.deal_analyses WHERE json_extract(payload,"
            "'$.request.snapshot.offer.source_name')=? AND "
            "json_extract(payload,'$.request.snapshot.offer.external_id')=?"
        )
        with self.connection() as db:
            params = (offer.source_name, offer.external_id)
            total = db.execute("SELECT COUNT(*) " + query, params).fetchone()[0]
            rows = db.execute(
                "SELECT payload "
                + query
                + " ORDER BY julianday(json_extract(payload,'$.request.as_of')) DESC,"
                "analysis_id DESC "
                "LIMIT 20 OFFSET ?",
                (*params, (page - 1) * 20),
            ).fetchall()
        return [AnalysisRecord.model_validate_json(row[0]) for row in rows], total

    def dashboard(self) -> dict[str, object]:
        with self.connection() as db:
            rows = db.execute(
                LATEST + "SELECT json_extract(analysis,'$.state') AS state,"
                "json_extract(analysis,'$.score.total') AS score FROM inventory"
            ).fetchall()
            demo = db.execute("SELECT COUNT(*) FROM offers WHERE is_demo=1").fetchone()[0]
            sent = db.execute(
                "SELECT is_demo,COUNT(*) FROM operations.alert_outbox "
                "WHERE status='sent' GROUP BY is_demo"
            ).fetchall()
        scores = [Decimal(row["score"]) for row in rows if row["score"] is not None]
        counts = dict(sent)
        return {
            "total": len(rows),
            "new": sum(row["state"] in (None, "NEW") for row in rows),
            "rejected": sum(row["state"] == DealState.REJECTED for row in rows),
            "deals": sum(row["state"] in (DealState.APPROVED, DealState.NOTIFIED) for row in rows),
            "avg_score": sum(scores) / len(scores) if scores else None,
            "demo_count": demo,
            "sent": counts.get(0, 0),
            "demo_sent": counts.get(1, 0),
            **self.ai_totals(self.settings.ai.db_path),
        }

    @staticmethod
    def ai_totals(path: Path) -> dict[str, object]:
        empty: dict[str, object] = {
            "ai_count": 0,
            "ai_cost": Decimal(0),
            "ai_reserved": Decimal(0),
            "ai_unknown": 0,
        }
        if not path.exists():
            return empty
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_attempts'").fetchone():
                return empty
            count = db.execute(
                "SELECT COUNT(*) FROM ai_reviews WHERE status='complete'"
            ).fetchone()[0]
            known, reserved, unknown = db.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN usage_known=1 THEN accounted_nanos ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN usage_known=0 THEN accounted_nanos ELSE 0 END),0),"
                "COALESCE(SUM(1-usage_known),0) FROM ai_attempts"
            ).fetchone()
            return {
                "ai_count": count,
                "ai_cost": Decimal(known) / Decimal(10**9),
                "ai_reserved": Decimal(reserved) / Decimal(10**9),
                "ai_unknown": unknown,
            }
        finally:
            db.close()

    def system(self) -> dict[str, object]:
        with self.connection() as db:
            jobs = db.execute(
                "SELECT payload FROM operations.job_runs ORDER BY started_at DESC LIMIT 20"
            ).fetchall()
            attempts = db.execute(
                "SELECT reason,status,created_at FROM operations.alert_outbox "
                "WHERE status IN ('failed','uncertain') ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            errors = db.execute(
                "SELECT payload FROM analyses.deal_analyses WHERE "
                "json_extract(payload,'$.state')='ERROR' ORDER BY "
                "julianday(json_extract(payload,'$.request.as_of')) DESC LIMIT 20"
            ).fetchall()
        return {
            "jobs": [CycleReport.model_validate_json(row[0]) for row in jobs],
            "notification_errors": [dict(row) for row in attempts],
            "analysis_errors": [AnalysisRecord.model_validate_json(row[0]) for row in errors],
        }
