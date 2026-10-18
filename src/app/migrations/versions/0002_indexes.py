"""Indexes for histories and operational status, without altering payloads."""

from alembic import op

revision = "0002_indexes"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = op.get_context().config
    assert config is not None
    role = config.attributes["role"]
    if role == "analyses":
        op.execute(
            "CREATE INDEX IF NOT EXISTS analysis_offer_history ON deal_analyses ("
            "json_extract(payload,'$.request.snapshot.offer.source_name'),"
            "json_extract(payload,'$.request.snapshot.offer.external_id'),"
            "julianday(json_extract(payload,'$.request.as_of')) DESC)"
        )
    if role == "operations":
        op.execute("CREATE INDEX IF NOT EXISTS job_started ON job_runs(started_at DESC)")
        op.execute(
            "CREATE INDEX IF NOT EXISTS feedback_offer ON admin_feedback_history "
            "(source_name,external_id,id DESC)"
        )
        op.execute("CREATE INDEX IF NOT EXISTS alert_status ON alert_outbox(status,created_at)")


def downgrade() -> None:
    raise RuntimeError("Restore an offline backup instead of downgrading")
