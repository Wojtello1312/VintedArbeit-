import json
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.cli import main
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository


@pytest.mark.parametrize(
    ("extension", "media_type"), [("json", "application/json"), ("csv", "text/csv")]
)
def test_http_import_twice(settings: Settings, extension: str, media_type: str) -> None:
    content = files("vinted_deal_hunter").joinpath(f"data/demo_offers.{extension}").read_bytes()
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.jobs_api_token.get_secret_value()
        response = client.post(
            f"/imports?format={extension}", content=content, headers={"Content-Type": media_type}
        )
        assert response.status_code == 200
        report = response.json()
        assert (report["new"], report["skipped"], report["errors"]) == (110, 10, 10)
        assert report["source_name"] == "demo"
        assert report["is_demo"] is True
        response = client.post(
            f"/imports?format={extension}", content=content, headers={"Content-Type": media_type}
        )
        assert response.status_code == 200
        assert response.json()["new"] == 0
        assert response.json()["skipped"] == 120
    # App restart must reuse persisted identity keys.
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.jobs_api_token.get_secret_value()
        assert (
            client.post(
                f"/imports?format={extension}",
                content=content,
                headers={"Content-Type": media_type},
            ).json()["new"]
            == 0
        )


def test_http_invalid_document_and_limits(settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"max_import_bytes": 20}))) as client:
        client.headers["Authorization"] = "Bearer " + settings.jobs_api_token.get_secret_value()
        assert (
            client.post(
                "/imports", content=b"[", headers={"Content-Type": "application/json"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/imports", content=b"[" * 21, headers={"Content-Type": "application/json"}
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/imports", content=b"[]", headers={"Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        assert client.post("/imports?format=xml", content=b"[]").status_code == 422
    with TestClient(create_app(settings.model_copy(update={"max_import_records": 1}))) as client:
        client.headers["Authorization"] = "Bearer " + settings.jobs_api_token.get_secret_value()
        assert (
            client.post(
                "/imports", content=b"[{},{}]", headers={"Content-Type": "application/json"}
            ).status_code
            == 422
        )


def test_http_storage_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, raw_offer: dict[str, object]
) -> None:
    def fail(*args, **kwargs):
        raise OperationalError("secret database details", {}, None)

    monkeypatch.setattr(SQLiteOfferRepository, "add_many", fail)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.jobs_api_token.get_secret_value()
        response = client.post("/imports", json=[raw_offer])
        assert response.status_code == 503
        assert "secret database details" not in response.text


def test_cli_mock_and_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "cli.sqlite3")
    assert main(["--db", db]) == 1  # Deliberate invalid demo records are reported.
    report = json.loads(capsys.readouterr().out)
    assert (report["new"], report["skipped"], report["errors"]) == (110, 10, 10)
    assert main(["--db", db]) == 1
    assert json.loads(capsys.readouterr().out)["new"] == 0
    assert main(["--db", db, "--format", "json", "--file", str(tmp_path / "absent")]) == 2


@pytest.mark.parametrize("extension", ["json", "csv"])
def test_cli_file_import(
    extension: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    resource = files("vinted_deal_hunter").joinpath(f"data/demo_offers.{extension}")
    path = tmp_path / f"demo.{extension}"
    path.write_bytes(resource.read_bytes())
    args = ["--format", extension, "--file", str(path), "--db", str(tmp_path / "cli.sqlite3")]
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["new"] == 110
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["skipped"] == 120


def test_cli_clean_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], raw_offer: dict[str, object]
) -> None:
    path = tmp_path / "clean.json"
    path.write_text(json.dumps([raw_offer]))
    assert main(["--format", "json", "--file", str(path), "--db", str(tmp_path / "db")]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == 0


def test_settings_environment_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VDH_MAX_IMPORT_RECORDS", "5")
    assert Settings.from_env().max_import_records == 5
    monkeypatch.setenv("VDH_MAX_IMPORT_RECORDS", "0")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.from_env()
