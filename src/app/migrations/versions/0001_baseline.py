"""Adopt the stage 7 schema without deleting existing data."""

import json
from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    assert config is not None
    role = config.attributes["role"]
    schema = json.loads(Path(__file__).parents[1].joinpath("baseline.json").read_text())[role]
    connection = op.get_bind()
    for table in schema["tables"]:
        existing = connection.exec_driver_sql(f'PRAGMA table_info("{table["name"]}")').fetchall()
        if existing and [list(row) for row in existing] != table["columns"]:
            raise RuntimeError("Unsupported legacy schema; restore backup or review migration")
        if not existing:
            op.execute(table["sql"])
    for statement in schema["indexes"]:
        op.execute(statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
    if role == "operations":
        op.execute("INSERT OR IGNORE INTO job_control VALUES (1,0)")
    op.execute("CREATE TABLE database_role (role TEXT PRIMARY KEY NOT NULL)")
    connection.exec_driver_sql("INSERT INTO database_role VALUES (?)", (role,))


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are disabled; restore an offline backup")
