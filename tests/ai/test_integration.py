from unittest.mock import Mock

import pytest

from app.ai.client import ClientResult, ReviewClientError
from app.ai.reviewer import AIReviewer, ReviewedDealPipeline
from app.ai.schemas import AIReview
from app.core.deal_pipeline import DealPipeline
from app.deal_config import DealConfig
from app.schemas.deals import DealState, RiskLevel
from app.storage.sqlite import SQLiteAnalysisStore


def orchestrator(pipeline, config, client, now, *, api_key="TEST-NOT-A-REAL-KEY"):
    return ReviewedDealPipeline(
        pipeline,
        AIReviewer(config, api_key=api_key, client=client, clock=lambda: now, sleep=Mock()),
    )


@pytest.mark.parametrize(
    "ai_level, state",
    [
        ("low", DealState.APPROVED),
        ("medium", DealState.APPROVED),
        ("high", DealState.REVIEW_REQUIRED),
        ("unknown", DealState.REVIEW_REQUIRED),
    ],
)
def test_integrated_risk_price_and_idempotency(
    ai_level, state, ai_config, fake_client, api_result, review_data, now, pipeline, ai_request
):
    original = pipeline.process(ai_request)
    fake_client.review.return_value = ClientResult(
        AIReview.model_validate({**review_data, "risk_level": ai_level}), api_result.usage, "test"
    )
    service = orchestrator(pipeline, ai_config, fake_client, now)
    record = service.process(ai_request)
    assert record.state == state
    assert record.score == original.score and record.estimate == original.estimate
    assert record.profit == original.profit
    assert set(flag.code for flag in original.risk.flags) <= {
        flag.code for flag in record.risk.flags
    }
    assert record.history[-1].previous == DealState.AI_PENDING
    assert record.ai_review.status == "completed"
    if ai_level == "high":
        assert record.risk.level == RiskLevel.HIGH and record.risk.requires_review
    assert service.process(ai_request) == record
    assert fake_client.review.call_count == 1


def test_no_key_and_service_error_use_deterministic_only(
    ai_config, fake_client, now, pipeline, ai_request
):
    service = orchestrator(pipeline, ai_config, fake_client, now, api_key=None)
    record = service.process(ai_request)
    assert record.state == DealState.APPROVED
    assert record.ai_review.reason == "missing_api_key" and record.ai_review.review is None
    fake_client.review.assert_not_called()
    other_pipeline = DealPipeline(
        pipeline.store, DealConfig(ai_review_required=True, approval_score=71), clock=lambda: now
    )
    fake_client.review.side_effect = ReviewClientError("http_401")
    record = orchestrator(other_pipeline, ai_config, fake_client, now).process(ai_request)
    assert record.state == DealState.APPROVED and record.ai_review.status == "unavailable"


def test_rejected_offer_never_reaches_ai(ai_config, fake_client, now, pipeline, ai_request):
    strict_pipeline = DealPipeline(
        pipeline.store, DealConfig(ai_review_required=True, approval_score=100), clock=lambda: now
    )
    record = orchestrator(strict_pipeline, ai_config, fake_client, now).process(ai_request)
    assert record.state == DealState.REJECTED and record.ai_review is None
    fake_client.review.assert_not_called()


def test_sqlite_checkpoint_restarts_without_second_review(
    tmp_path, ai_config, fake_client, now, ai_request
):
    path = tmp_path / "checkpoints.sqlite3"
    store = SQLiteAnalysisStore(path)
    pipeline = DealPipeline(store, DealConfig(ai_review_required=True), clock=lambda: now)
    record = orchestrator(pipeline, ai_config, fake_client, now).process(ai_request)
    store.close()
    store = SQLiteAnalysisStore(path)
    restarted = DealPipeline(store, DealConfig(ai_review_required=True), clock=lambda: now)
    assert orchestrator(restarted, ai_config, fake_client, now).process(ai_request) == record
    assert fake_client.review.call_count == 1
    store.close()


def test_cache_recovers_crash_between_review_and_checkpoint(
    ai_config, fake_client, now, pipeline, ai_request, pending
):
    reviewer = AIReviewer(ai_config, api_key="TEST", client=fake_client, clock=lambda: now)
    assert reviewer.review(pending).status == "completed"
    assert pipeline.store.get(pending.analysis_id).state == DealState.AI_PENDING
    result = orchestrator(pipeline, ai_config, fake_client, now).process(ai_request)
    assert result.state == DealState.APPROVED and result.ai_review.status == "cached"
    assert fake_client.review.call_count == 1


def test_low_ai_risk_does_not_reduce_deterministic_risk(
    ai_config, fake_client, now, pipeline, ai_request
):
    request = ai_request.model_copy(
        update={"snapshot": ai_request.snapshot.model_copy(update={"demand": None})}
    )
    pending = pipeline.process(request)
    assert pending.state == DealState.AI_PENDING and pending.risk.level == RiskLevel.MEDIUM
    record = orchestrator(pipeline, ai_config, fake_client, now).process(request)
    assert record.state == DealState.APPROVED
    assert record.risk == pending.risk


def test_batch_keeps_independent_items_and_disabled_default(
    ai_config, fake_client, now, pipeline, ai_request
):
    service = ReviewedDealPipeline(pipeline, AIReviewer())
    result = service.process_batch([ai_request, ai_request])
    assert not result.failures
    assert len(result.records) == 2 and result.records[0] == result.records[1]
    assert result.records[0].state == DealState.APPROVED
    assert result.records[0].ai_review.reason == "disabled"
    fake_client.review.assert_not_called()


def test_inflight_review_does_not_prematurely_approve(
    ai_config, fake_client, now, pipeline, ai_request, pending
):
    from app.ai.budget import AIBudget
    from app.ai.prompts import PROMPT_VERSION, input_hash, review_payload

    budget = AIBudget(ai_config)
    budget.claim(
        input_hash(review_payload(pending), ai_config.model), ai_config.model, PROMPT_VERSION, now
    )
    record = orchestrator(pipeline, ai_config, fake_client, now).process(ai_request)
    assert record.state == DealState.AI_PENDING and record.ai_review is None
    fake_client.review.assert_not_called()


def test_decision_rejects_pending_or_nonqualified_completion(
    ai_config, fake_client, now, pipeline, ai_request, pending
):
    from app.ai.schemas import ReviewOutcome

    with pytest.raises(ValueError, match="active"):
        pipeline.complete_ai_review(
            pending.analysis_id, ReviewOutcome(status="pending", reason="active")
        )
    strict_pipeline = DealPipeline(
        pipeline.store, DealConfig(ai_review_required=True, approval_score=100), clock=lambda: now
    )
    rejected = strict_pipeline.process(ai_request)
    with pytest.raises(ValueError, match="qualified"):
        strict_pipeline.complete_ai_review(
            rejected.analysis_id, ReviewOutcome(status="skipped", reason="disabled")
        )


def test_crash_before_api_does_not_create_duplicate_call(
    ai_config, fake_client, now, pipeline, ai_request, pending
):
    from datetime import timedelta

    fake_client.review.side_effect = KeyboardInterrupt()
    service = orchestrator(pipeline, ai_config, fake_client, now)
    with pytest.raises(KeyboardInterrupt):
        service.process(ai_request)
    assert pipeline.store.get(pending.analysis_id).state == DealState.AI_PENDING
    resumed = orchestrator(pipeline, ai_config, fake_client, now + timedelta(hours=1))
    result = resumed.process(ai_request)
    assert result.state == DealState.APPROVED
    assert result.ai_review.reason == "previous_attempt_unavailable"
    assert fake_client.review.call_count == 1
