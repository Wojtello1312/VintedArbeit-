"""Short atomic SQLite transactions for local operational state."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.schemas.pricing import utc_timestamp
from app.storage.migrations import ensure_database


def timestamp(now: datetime) -> str:
    return utc_timestamp(now).isoformat(timespec="microseconds")


class OperationsStore:
    def __init__(self, path: Path) -> None:
        ensure_database(path, "operations")
        self.path = path

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
