import json
import logging
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.ai.budget import AIBudget, BudgetUnavailable, usage_cost
from app.ai.client import ClientResult, ReviewClientError
from app.ai.config import AIConfig
from app.ai.prompts import PROMPT_VERSION, input_hash, review_payload
from app.ai.reviewer import AIReviewer
from app.ai.schemas import AIReview
from app.schemas.deals import DealState
from app.schemas.pricing import Confidence
from vinted_deal_hunter.config import Settings


def make_reviewer(config, client, now, **kwargs):
    return AIReviewer(
        config,
        api_key="TEST-NOT-A-REAL-KEY",
        client=client,
        clock=lambda: now,
        sleep=Mock(),
        **kwargs,
    )


def test_defaults_and_env(monkeypatch):
    assert not AIConfig().enabled
    assert AIConfig().model == "gpt-5-nano"
    assert not AIConfig().pricing_configured
    monkeypatch.setenv("OPENAI_MODEL", "configured-model")
    monkeypatch.setenv("VDH_AI", '{"enabled":true,"daily_budget_usd":"0.50"}')
    settings = Settings.from_env()
    assert settings.ai.model == "configured-model"
    assert settings.ai.enabled
    assert settings.ai.daily_budget_usd == Decimal("0.50")
    assert "api_key" not in settings.model_dump_json().lower()
    assert AIReviewer.from_env().config == settings.ai


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"enabled": False}, "disabled"),
        ({"allow_demo": False}, "demo_offline"),
        ({"input_usd_per_million": None}, "pricing_unconfigured"),
        ({"min_score": "100"}, "score_below_ai_threshold"),
        ({"min_valuation_confidence": 1}, "unreliable_valuation"),
        ({"min_comparisons": 1000}, "unreliable_valuation"),
        ({"max_input_tokens": 1000}, "input_too_large"),
        ({"daily_budget_usd": "0"}, "budget_exhausted"),
        ({"max_daily_analyses": 0}, "budget_exhausted"),
    ],
)
def test_gates_no_api(changes, reason, ai_config, fake_client, now, pending):
    config = AIConfig.model_validate({**ai_config.model_dump(), **changes})
    outcome = make_reviewer(config, fake_client, now).review(pending)
    assert outcome.reason == reason
    assert outcome.review is None
    fake_client.review.assert_not_called()


@pytest.mark.parametrize("state", [state for state in DealState if state != DealState.AI_PENDING])
def test_only_pending_can_call(state, ai_config, fake_client, now, pending):
    reviewer = make_reviewer(ai_config, fake_client, now)
    assert reviewer.review(pending.model_copy(update={"state": state})).status == "skipped"
    fake_client.review.assert_not_called()


def test_missing_key_and_low_confidence_label(ai_config, fake_client, now, pending):
    reviewer = AIReviewer(ai_config, client=fake_client, clock=lambda: now)
    assert reviewer.review(pending).reason == "missing_api_key"
    reviewer = make_reviewer(ai_config, fake_client, now)
    record = pending.model_copy(
        update={"estimate": pending.estimate.model_copy(update={"confidence": Confidence.LOW})}
    )
    assert reviewer.review(record).reason == "unreliable_valuation"
    fake_client.review.assert_not_called()


@pytest.mark.parametrize("score, calls", [("64.999", 0), ("65", 1), ("65.001", 1)])
def test_exact_score_boundary(score, calls, ai_config, fake_client, now, pending):
    record = pending.model_copy(
        update={"score": pending.score.model_copy(update={"total": Decimal(score)})}
    )
    make_reviewer(ai_config, fake_client, now).review(record)
    assert fake_client.review.call_count == calls


def test_success_and_persistent_cache(ai_config, fake_client, api_result, now, pending, caplog):
    caplog.set_level(logging.INFO)
    reviewer = make_reviewer(ai_config, fake_client, now)
    first = reviewer.review(pending)
    assert first.status == "completed"
    assert first.review.result == api_result.review
    assert first.review.model == "gpt-5-nano-test-snapshot"
    assert first.review.prompt_version == PROMPT_VERSION
    assert first.review.reviewed_at == now
    assert first.review.cost_usd == Decimal("0.00135")
    assert first.review.cost_usd == usage_cost(api_result.usage, ai_config)
    assert "input_tokens=1000" in caplog.text
    assert "cost_usd=0.00135" in caplog.text
    assert "TEST-NOT-A-REAL-KEY" not in caplog.text
    second_client = Mock()
    second = make_reviewer(ai_config, second_client, now).review(pending)
    assert second.status == "cached" and second.review == first.review
    second_client.review.assert_not_called()
    assert fake_client.review.call_count == 1
    assert AIBudget(ai_config).daily_usage(now).analyses == 1
    assert AIBudget(ai_config).daily_usage(now).unknown_attempts == 0


def test_hash_tracks_content_model_and_excludes_only_volatile_time(pending):
    payload = review_payload(pending)
    shifted = pending.model_copy(
        update={
            "estimate": pending.estimate.model_copy(
                update={"calculated_at": pending.estimate.calculated_at + timedelta(minutes=1)}
            ),
            "request": pending.request.model_copy(
                update={"observations": tuple(reversed(pending.request.observations))}
            ),
        }
    )
    assert review_payload(shifted) == payload
    assert input_hash(payload, "a") != input_hash(payload, "b")
    changed = pending.model_copy(
        update={
            "request": pending.request.model_copy(
                update={
                    "snapshot": pending.request.snapshot.model_copy(
                        update={"description": "DEMO changed"}
                    )
                }
            )
        }
    )
    assert input_hash(review_payload(changed), "a") != input_hash(payload, "a")
    data = json.loads(payload)
    assert len(data["comparables"]) == pending.estimate.sample_size
    assert data["untrusted_listing"]["offer"]["is_demo"] is True


def test_retry_backoff_metered_each_attempt(ai_config, fake_client, api_result, now, pending):
    fake_client.review.side_effect = [
        ReviewClientError("http_429", transient=True, retry_after=2),
        ReviewClientError("http_503", transient=True),
        api_result,
    ]
    reviewer = make_reviewer(ai_config, fake_client, now)
    result = reviewer.review(pending)
    assert result.status == "completed"
    assert [call.args[0] for call in reviewer.sleep.call_args_list] == [2, 2]
    usage = reviewer.budget.daily_usage(now)
    assert usage.analyses == 1 and usage.attempts == 3 and usage.unknown_attempts == 2
    assert usage.accounted_usd == 2 * reviewer.budget.reservation_usd + result.review.cost_usd


@pytest.mark.parametrize(
    "transient, retry_after, attempts", [(False, 0, 1), (True, 0, 3), (True, 999, 1)]
)
def test_bounded_failure_and_no_repeat_after_restart(
    transient, retry_after, attempts, ai_config, fake_client, now, pending
):
    fake_client.review.side_effect = ReviewClientError(
        "safe_error", transient=transient, retry_after=retry_after
    )
    reviewer = make_reviewer(ai_config, fake_client, now)
    assert reviewer.review(pending).status == "unavailable"
    assert fake_client.review.call_count == attempts
    assert reviewer.budget.daily_usage(now).unknown_attempts == attempts
    assert make_reviewer(ai_config, fake_client, now).review(pending).reason == (
        "previous_attempt_unavailable"
    )
    assert fake_client.review.call_count == attempts


def test_retry_cannot_exceed_budget(ai_config, fake_client, now, pending):
    config = ai_config.model_copy(update={"daily_budget_usd": AIBudget(ai_config).reservation_usd})
    fake_client.review.side_effect = ReviewClientError("timeout", transient=True)
    reviewer = make_reviewer(config, fake_client, now)
    assert reviewer.review(pending).reason == "retry_budget_exhausted"
    assert fake_client.review.call_count == 1


@pytest.mark.parametrize("reason", ["incomplete_or_refused", "invalid_response"])
def test_no_review_is_not_success(reason, ai_config, fake_client, api_result, now, pending):
    fake_client.review.return_value = ClientResult(None, api_result.usage, "test-model", reason)
    outcome = make_reviewer(ai_config, fake_client, now).review(pending)
    assert outcome.status == "unavailable" and outcome.review is None
    assert fake_client.review.call_count == 1


def test_unknown_usage_remains_reserved(ai_config, fake_client, api_result, now, pending):
    fake_client.review.return_value = ClientResult(api_result.review, None, "test-model")
    reviewer = make_reviewer(ai_config, fake_client, now)
    outcome = reviewer.review(pending)
    assert outcome.status == "completed" and outcome.review.cost_usd is None
    usage = reviewer.budget.daily_usage(now)
    assert usage.unknown_attempts == 1
    assert usage.accounted_usd == reviewer.budget.reservation_usd


def test_storage_failure_before_call_fails_closed(ai_config, fake_client, now, pending):
    budget = AIBudget(ai_config)
    budget.claim = Mock(side_effect=BudgetUnavailable())
    outcome = make_reviewer(ai_config, fake_client, now, budget=budget).review(pending)
    assert outcome.status == "unavailable"
    fake_client.review.assert_not_called()


def test_storage_failure_after_high_risk_does_not_erase_result(
    ai_config, fake_client, api_result, review_data, now, pending
):
    review = AIReview.model_validate({**review_data, "risk_level": "high"})
    fake_client.review.return_value = ClientResult(review, api_result.usage, "test-model")
    budget = AIBudget(ai_config)
    budget.settle = Mock(side_effect=BudgetUnavailable())
    budget.finish = Mock(side_effect=BudgetUnavailable())
    result = make_reviewer(ai_config, fake_client, now, budget=budget).review(pending)
    assert result.manual_review_required and result.review.result.risk_level == "high"
    assert result.review.cost_usd is None


def test_low_confidence_and_inconsistency_require_review(
    ai_config, fake_client, api_result, review_data, now, pending
):
    review = AIReview.model_validate({**review_data, "confidence": 0.59, "listing_consistency": 69})
    fake_client.review.return_value = ClientResult(review, api_result.usage, "test-model")
    assert make_reviewer(ai_config, fake_client, now).review(pending).manual_review_required


def test_cached_high_risk_survives_missing_key(
    ai_config, fake_client, api_result, review_data, now, pending
):
    fake_client.review.return_value = ClientResult(
        AIReview.model_validate({**review_data, "risk_level": "high"}), api_result.usage, "test"
    )
    first = make_reviewer(ai_config, fake_client, now).review(pending)
    assert first.manual_review_required
    without_key = AIReviewer(ai_config, clock=lambda: now)
    cached = without_key.review(pending)
    assert cached.status == "cached" and cached.manual_review_required
    assert cached.review == first.review
    assert fake_client.review.call_count == 1


def test_prompt_uses_only_latest_comparable_snapshot(pending):
    reference = pending.estimate.included_observations[0]
    current = next(
        item
        for item in pending.request.observations
        if item.observation.external_id == reference.external_id
    )
    old = current.model_copy(
        update={
            "available_at": current.available_at - timedelta(seconds=1),
            "observation": current.observation.model_copy(
                update={
                    "features": current.observation.features.model_copy(
                        update={"model": "old model"}
                    )
                }
            ),
        }
    )
    changed = pending.model_copy(
        update={
            "request": pending.request.model_copy(
                update={"observations": (*pending.request.observations, old)}
            )
        }
    )
    assert review_payload(changed) == review_payload(pending)
