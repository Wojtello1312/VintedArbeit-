"""Track arrival order and persist local removal across repeated imports."""

from alembic import op

revision = "0003_offer_management"
down_revision = "0002_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    assert config is not None
    if config.attributes["role"] == "offers":
        op.execute("ALTER TABLE offers ADD COLUMN imported_at TEXT")
        op.execute(
            "CREATE TABLE deleted_offers (source_name TEXT NOT NULL, "
            "external_id TEXT NOT NULL, deleted_at TEXT NOT NULL, "
            "PRIMARY KEY (source_name, external_id))"
        )
        op.execute("CREATE INDEX offers_arrival ON offers(imported_at DESC)")


def downgrade() -> None:
    raise RuntimeError("Restore an offline backup instead of downgrading")
