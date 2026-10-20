"""Serialized, transactional Alembic upgrades; never silently stamp an unknown schema."""

from pathlib import Path
from threading import RLock
from typing import Literal

import portalocker
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

DatabaseRole = Literal["offers", "analyses", "operations", "ai"]
HEAD = "0003_offer_management"
_ALEMBIC_LOCK = RLock()


def ensure_database(path: Path, role: DatabaseRole) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path) + ".migration.lock", timeout=10):
        engine = create_engine(
            URL.create("sqlite", database=str(path)), connect_args={"timeout": 10}
        )
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                tables = set(
                    connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).scalars()
                )
                if "database_role" in tables:
                    saved = connection.exec_driver_sql("SELECT role FROM database_role").scalar()
                    if saved != role:
                        raise RuntimeError("Database path reused for a different role")
                if "alembic_version" in tables:
                    current = connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalar()
                    if current == HEAD and "database_role" in tables:
                        connection.commit()
                        return
                config = Config()
                config.set_main_option(
                    "script_location", str(Path(__file__).parents[1] / "migrations")
                )
                config.attributes.update(connection=connection, role=role)
                with _ALEMBIC_LOCK:
                    command.upgrade(config, "head")
                connection.commit()
        finally:
            engine.dispose()
