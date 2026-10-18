from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.ai.budget import AIBudget, usage_cost
from app.ai.config import AIConfig
from app.ai.schemas import TokenUsage


def test_atomic_limits_across_workers_and_restart(ai_config, now):
    config = ai_config.model_copy(update={"max_daily_analyses": 3})
    budget = AIBudget(config)

    def worker(index):
        return AIBudget(config).claim(str(index), config.model, "test", now)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(16)))
    assert sum(result.status == "reserved" for result in results) == 3
    assert budget.daily_usage(now).analyses == 3
    assert AIBudget(config).claim("new", config.model, "test", now).status == "budget_exhausted"
    next_day = now + timedelta(days=1)
    assert AIBudget(config).claim("tomorrow", config.model, "test", next_day).status == "reserved"
    assert budget.daily_usage(next_day).analyses == 1


def test_same_hash_can_only_be_reserved_once(ai_config, now):
    AIBudget(ai_config)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: AIBudget(ai_config).claim("same", "test", "test", now), range(16))
        )
    assert sum(result.status == "reserved" for result in results) == 1
    assert sum(result.status == "pending" for result in results) == 15
    assert AIBudget(ai_config).daily_usage(now).attempts == 1


def test_dollar_limit_exact_boundary(ai_config, now):
    reserve = AIBudget(ai_config).reservation_usd
    config = ai_config.model_copy(update={"daily_budget_usd": reserve})
    budget = AIBudget(config)
    claim = budget.claim("one", "test", "test", now)
    assert claim.status == "reserved"
    assert budget.claim("two", "test", "test", now).status == "budget_exhausted"
    budget.settle(
        claim.attempt_id, TokenUsage(input_tokens=100, output_tokens=0, cached_input_tokens=0)
    )
    assert budget.daily_usage(now).accounted_usd == Decimal("0.0001")
    # The remaining balance still cannot cover a whole additional reservation.
    assert budget.claim("two", "test", "test", now).status == "budget_exhausted"


def test_crashed_worker_never_causes_an_automatic_duplicate(ai_config, now):
    budget = AIBudget(ai_config)
    assert budget.claim("crash", "test", "test", now).status == "reserved"
    assert budget.claim("crash", "test", "test", now).status == "pending"
    late = now + timedelta(hours=1)
    assert AIBudget(ai_config).claim("crash", "test", "test", late).status == (
        "previous_attempt_unavailable"
    )
    assert budget.daily_usage(now).accounted_usd == budget.reservation_usd
    assert budget.daily_usage(now).unknown_attempts == 1


def test_retry_counts_on_utc_day_and_obeys_new_day_count_limit(ai_config):
    from datetime import datetime

    before = datetime.fromisoformat("2026-01-01T23:59:59+00:00")
    after = datetime.fromisoformat("2026-01-02T01:00:00+01:00")
    budget = AIBudget(ai_config)
    budget.claim("test", "test", "test", before)
    assert budget.reserve_retry("test", after) is not None
    assert budget.daily_usage(before).attempts == 1
    assert budget.daily_usage(after).attempts == 1


def test_no_float_money_or_invalid_limits():
    for data in (
        {"daily_budget_usd": 0.1},
        {"daily_budget_usd": "-1"},
        {"min_score": 64},
        {"max_attempts": 0},
        {"max_daily_analyses": -1},
        {"timeout_seconds": 0},
        {"output_usd_per_million": "0"},
        {"model": " "},
        {"daily_budget_usd": "NaN"},
    ):
        with pytest.raises(ValidationError):
            AIConfig.model_validate(data)


def test_cached_tokens_and_nano_rounding(ai_config):
    usage = TokenUsage(input_tokens=1, output_tokens=1, cached_input_tokens=1)
    assert usage_cost(usage, ai_config) == Decimal("0.0000025")
    config = ai_config.model_copy(update={"cached_input_usd_per_million": None})
    assert usage_cost(usage, config) == Decimal("0.000003")
    config = ai_config.model_copy(update={"output_usd_per_million": Decimal("0.0001")})
    assert usage_cost(
        TokenUsage(input_tokens=0, output_tokens=1, cached_input_tokens=0), config
    ) == (Decimal("0.000000001"))
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=1, output_tokens=0, cached_input_tokens=2)
