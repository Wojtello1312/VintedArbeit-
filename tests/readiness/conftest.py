import httpx
import pytest
from pydantic import SecretStr

from app.admin.password import hash_password
from app.scheduler.config import SchedulerConfig
from vinted_deal_hunter.config import Settings


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External HTTP is forbidden in readiness tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(scope="session")
def hashed_password():
    return hash_password("test-readiness-password")


@pytest.fixture
def settings(tmp_path, hashed_password):
    return Settings(
        db_path=tmp_path / "offers.sqlite3",
        scheduler=SchedulerConfig(enabled=False),
        jobs_api_token=SecretStr("readiness-test-token-" + "x" * 32),
        admin_password_hash=SecretStr(hashed_password),
    )


@pytest.fixture
def auth(settings):
    return {"Authorization": "Bearer " + settings.jobs_api_token.get_secret_value()}
