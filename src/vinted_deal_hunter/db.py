"""Minimal ingestion storage; full observation history/migrations are a later stage."""

import hashlib
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy import BigInteger, Boolean, CheckConstraint, String, Text, create_engine, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.storage.migrations import ensure_database
from vinted_deal_hunter.models import OfferInput


class Base(DeclarativeBase):
    pass


class OfferRow(Base):
    __tablename__ = "offers"
    __table_args__ = (CheckConstraint("price_minor > 0"),)

    source_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class Disposition(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"
    CHANGED_EXISTING = "changed_existing"
    PROVENANCE_CONFLICT = "provenance_conflict"


class OfferRepository(Protocol):
    def add_many(self, offers: Sequence[OfferInput]) -> list[Disposition]: ...


class SQLiteOfferRepository:
    def __init__(self, path: Path) -> None:
        ensure_database(path, "offers")
        self.engine = create_engine(
            URL.create("sqlite", database=str(path)), connect_args={"timeout": 10}
        )

    def close(self) -> None:
        self.engine.dispose()

    def add_many(self, offers: Sequence[OfferInput]) -> list[Disposition]:
        results: list[Disposition] = []
        with self.engine.begin() as connection:
            for offer in offers:
                payload = offer.model_dump_json()
                fingerprint = hashlib.sha256(payload.encode()).hexdigest()
                statement = (
                    insert(OfferRow)
                    .values(
                        source_name=offer.source_name,
                        external_id=offer.external_id,
                        is_demo=offer.is_demo,
                        price_minor=offer.price_minor,
                        fingerprint=fingerprint,
                        payload=payload,
                    )
                    .on_conflict_do_nothing(index_elements=["source_name", "external_id"])
                )
                result = connection.execute(statement)
                if result.rowcount == 1:
                    results.append(Disposition.NEW)
                    continue
                existing = connection.execute(
                    select(OfferRow.is_demo, OfferRow.fingerprint).where(
                        OfferRow.source_name == offer.source_name,
                        OfferRow.external_id == offer.external_id,
                    )
                ).one()
                if existing.is_demo != offer.is_demo:
                    results.append(Disposition.PROVENANCE_CONFLICT)
                elif existing.fingerprint != fingerprint:
                    results.append(Disposition.CHANGED_EXISTING)
                else:
                    results.append(Disposition.DUPLICATE)
        return results

    def list_offers(self) -> list[OfferInput]:
        with self.engine.connect() as connection:
            return [
                OfferInput.model_validate_json(payload)
                for payload in connection.scalars(
                    select(OfferRow.payload).order_by(OfferRow.source_name, OfferRow.external_id)
                )
            ]
