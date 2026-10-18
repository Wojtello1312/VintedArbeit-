"""Unauthenticated minimal probes; readiness checks storage, never paid services."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter
from starlette.responses import JSONResponse

from app.storage.migrations import HEAD
from vinted_deal_hunter.config import Settings

REQUIRED = {
    "offers": ("offers",),
    "analyses": ("deal_analyses",),
    "operations": (
        "job_runs",
        "job_control",
        "alert_outbox",
        "notification_slots",
        "admin_sessions",
        "admin_login_attempts",
        "admin_policy",
        "admin_feedback",
        "admin_feedback_history",
    ),
    "ai": ("ai_reviews", "ai_attempts"),
}


def database_paths(settings: Settings) -> dict[str, Path]:
    return {
        "offers": settings.db_path,
        "analyses": settings.db_path.with_name("deals.sqlite3"),
        "operations": settings.jobs_db_path,
        "ai": settings.ai.db_path,
    }


def storage_ready(settings: Settings) -> bool:
    try:
        for role, path in database_paths(settings).items():
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=1)
            try:
                if db.execute("SELECT version_num FROM alembic_version").fetchone() != (HEAD,):
                    return False
                if db.execute("SELECT role FROM database_role").fetchone() != (role,):
                    return False
                for table in REQUIRED[role]:
                    db.execute(f'SELECT 1 FROM "{table}" LIMIT 1')
                # Read-write schema probe rolled back: a read-only volume is not ready.
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE alembic_version SET version_num=version_num")
                db.rollback()
            finally:
                db.close()
        return True
    except (OSError, sqlite3.Error):
        return False


def health_router(settings: Settings) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @router.get("/health/ready")
    def ready() -> JSONResponse:
        good = storage_ready(settings)
        return JSONResponse(
            {"status": "ready" if good else "unavailable"}, status_code=200 if good else 503
        )

    return router
