"""SQLAlchemy checkpoint adapter using an atomic revision compare-and-set."""

from pathlib import Path

from sqlalchemy import Integer, String, Text, create_engine, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.ports import StoreUnavailable
from app.schemas.deals import AnalysisRecord
from app.storage.migrations import ensure_database


class CheckpointBase(DeclarativeBase):
    pass


class AnalysisRow(CheckpointBase):
    __tablename__ = "deal_analyses"
    analysis_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class SQLiteAnalysisStore:
    def __init__(self, path: Path) -> None:
        ensure_database(path, "analyses")
        self.engine = create_engine(
            URL.create("sqlite", database=str(path)), connect_args={"timeout": 10}
        )

    def close(self) -> None:
        self.engine.dispose()

    def create(self, record: AnalysisRecord) -> AnalysisRecord:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(AnalysisRow)
                    .values(
                        analysis_id=record.analysis_id,
                        revision=record.revision,
                        payload=record.model_dump_json(),
                    )
                    .on_conflict_do_nothing(index_elements=["analysis_id"])
                )
                payload = connection.scalar(
                    select(AnalysisRow.payload).where(AnalysisRow.analysis_id == record.analysis_id)
                )
                assert payload is not None
                return AnalysisRecord.model_validate_json(payload)
        except SQLAlchemyError as exc:
            raise StoreUnavailable("cannot create checkpoint") from exc

    def get(self, analysis_id: str) -> AnalysisRecord | None:
        try:
            with self.engine.connect() as connection:
                payload = connection.scalar(
                    select(AnalysisRow.payload).where(AnalysisRow.analysis_id == analysis_id)
                )
                return None if payload is None else AnalysisRecord.model_validate_json(payload)
        except SQLAlchemyError as exc:
            raise StoreUnavailable("cannot load checkpoint") from exc

    def compare_and_set(self, record: AnalysisRecord, expected_revision: int) -> bool:
        if record.revision != expected_revision + 1:
            raise ValueError("a commit must advance revision by exactly one")
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(AnalysisRow)
                    .where(
                        AnalysisRow.analysis_id == record.analysis_id,
                        AnalysisRow.revision == expected_revision,
                    )
                    .values(revision=record.revision, payload=record.model_dump_json())
                )
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise StoreUnavailable("cannot commit checkpoint") from exc
