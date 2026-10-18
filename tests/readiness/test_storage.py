import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.health import database_paths, storage_ready
from app.migrate import upgrade
from app.storage.migrations import HEAD, ensure_database
from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.config import Settings


def test_fresh_migration_and_repeat(settings):
    assert not storage_ready(settings)
    upgrade(settings)
    upgrade(settings)
    assert storage_ready(settings)
    for role, path in database_paths(settings).items():
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
            assert db.execute("SELECT role FROM database_role").fetchone() == (role,)
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_legacy_schema_adoption_preserves_rows(settings):
    schema = json.loads(files("app.migrations").joinpath("baseline.json").read_text())
    for role, path in database_paths(settings).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            for table in schema[role]["tables"]:
                db.execute(table["sql"])
            for sql in schema[role]["indexes"]:
                db.execute(sql)
            if role == "operations":
                db.execute("INSERT INTO job_control VALUES (1,1)")
                db.execute(
                    "INSERT INTO admin_feedback VALUES ('demo','DEMO-1','purchased','2026-01-01')"
                )
            elif role == "offers":
                db.execute("INSERT INTO offers VALUES ('demo','DEMO-1',1,4000,'fingerprint','{}')")
            elif role == "analyses":
                db.execute("INSERT INTO deal_analyses VALUES ('legacy',1,'{}')")
            elif role == "ai":
                db.execute(
                    "INSERT INTO ai_reviews VALUES ('old','mock','v1','2026-01-01','failed',NULL)"
                )
                db.execute(
                    "INSERT INTO ai_attempts VALUES "
                    "('attempt','old','2026-01-01',100,100,NULL,NULL,NULL,0)"
                )
    upgrade(settings)
    with sqlite3.connect(settings.jobs_db_path) as db:
        assert db.execute("SELECT paused FROM job_control").fetchone() == (1,)
        assert db.execute("SELECT label FROM admin_feedback").fetchone() == ("purchased",)
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT price_minor FROM offers").fetchone() == (4000,)
    with sqlite3.connect(settings.ai.db_path) as db:
        assert db.execute("SELECT accounted_nanos FROM ai_attempts").fetchone() == (100,)
    with sqlite3.connect(settings.db_path.with_name("deals.sqlite3")) as db:
        assert db.execute("SELECT revision FROM deal_analyses").fetchone() == (1,)


def test_migration_rejects_unknown_schema_without_stamping(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE offers (wrong TEXT)")
        db.execute("INSERT INTO offers VALUES ('keep-me')")
    with pytest.raises(RuntimeError, match="Unsupported legacy schema"):
        ensure_database(path, "offers")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT wrong FROM offers").fetchone() == ("keep-me",)
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='alembic_version'"
        ).fetchall()


def test_migration_serializes_parallel_startup(tmp_path):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: ensure_database(tmp_path / "db.sqlite3", "operations"), range(12)))
    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM job_control").fetchone() == (1,)


def test_wrong_database_role_rejected(tmp_path):
    path = tmp_path / "db.sqlite3"
    ensure_database(path, "offers")
    with pytest.raises(RuntimeError, match="different role"):
        ensure_database(path, "ai")
    with pytest.raises(ValidationError, match="distinct paths"):
        Settings(db_path=tmp_path / "jobs.sqlite3")


def test_health_is_minimal_and_detects_storage_damage(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        with sqlite3.connect(settings.ai.db_path) as db:
            db.execute("DROP TABLE ai_attempts")
        response = client.get("/health/ready")
        assert response.status_code == 503 and response.json() == {"status": "unavailable"}
        assert str(settings.ai.db_path) not in response.text
        assert client.get("/health/live").status_code == 200


def test_ai_database_follows_custom_data_location(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDH_DB_PATH", str(tmp_path / "custom" / "offers.sqlite3"))
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-nano")
    monkeypatch.setenv("VDH_AI", "{}")
    assert Settings.from_env().ai.db_path == tmp_path / "custom" / "ai.sqlite3"


def test_legacy_ai_ledger_cannot_be_silently_abandoned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / ".data" / "ai.sqlite3"
    ensure_database(legacy, "ai")
    monkeypatch.setenv("VDH_DB_PATH", str(tmp_path / "moved" / "offers.sqlite3"))
    monkeypatch.setenv("VDH_AI", "{}")
    with pytest.raises(ValueError, match="legacy AI ledger"):
        Settings.from_env()
    monkeypatch.setenv("VDH_AI", json.dumps({"db_path": str(legacy)}))
    assert Settings.from_env().ai.db_path == legacy
