from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.ai.budget import AIBudget
from app.ai.config import AIConfig
from vinted_deal_hunter.api import create_app


@pytest.mark.parametrize(
    "path,method",
    [("/imports", "post"), ("/offers", "get"), ("/jobs/status", "get"), ("/jobs/run", "post")],
)
def test_rest_requires_credentials(settings, path, method):
    with TestClient(create_app(settings)) as client:
        response = getattr(client, method)(path)
        assert response.status_code == 401
        assert (
            getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
    with TestClient(create_app(settings.model_copy(update={"jobs_api_token": None}))) as client:
        assert getattr(client, method)(path).status_code == 503


def test_atomic_usd_budget_across_threads_restart_and_unknown_cost(settings):
    config = AIConfig(
        db_path=settings.ai.db_path,
        max_daily_analyses=1000,
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("2"),
    )
    reservation = AIBudget(config).reservation_usd
    config = config.model_copy(update={"daily_budget_usd": reservation * 2})
    now = datetime.now(UTC)

    def claim(index):
        return AIBudget(config).claim(str(index), config.model, "test", now)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(20)))
    assert sum(c.status == "reserved" for c in claims) == 2
    assert sum(c.status == "budget_exhausted" for c in claims) == 18
    budget = AIBudget(config)
    assert budget.daily_usage(now).accounted_usd == reservation * 2
    assert budget.daily_usage(now).unknown_attempts == 2
    for c in claims:
        if c.attempt_id:
            assert budget.settle(c.attempt_id, None) is None
    assert AIBudget(config).claim("next", config.model, "test", now).status == "budget_exhausted"


def test_secret_values_never_returned_in_api_health_openapi(settings, auth):
    with TestClient(create_app(settings), headers=auth) as client:
        for path in ("/health/live", "/health/ready", "/openapi.json", "/offers", "/jobs/status"):
            response = client.get(path)
            assert response.status_code == 200
            assert settings.jobs_api_token.get_secret_value() not in response.text
            assert settings.admin_password_hash.get_secret_value() not in response.text


def test_nested_configuration_errors_do_not_echo_secret_values():
    from pydantic import ValidationError

    from vinted_deal_hunter.config import Settings

    with pytest.raises(ValidationError) as error:
        Settings(ai='{"api_key":"TEST-NOT-FOR-LOGS"}')
    assert "TEST-NOT-FOR-LOGS" not in str(error.value)
