"""Application service: gate, reserve, call, validate, account and cache optional AI."""

import logging
import os
import time
from collections.abc import Callable, Iterable
from datetime import datetime

from app.ai.budget import AIBudget, BudgetUnavailable
from app.ai.client import OpenAIReviewClient, ReviewClient, ReviewClientError
from app.ai.config import AIConfig
from app.ai.prompts import PROMPT_VERSION, input_hash, input_token_upper_bound, review_payload
from app.ai.schemas import AIReview, ReviewOutcome, StoredReview
from app.core.deal_pipeline import DealPipeline
from app.core.price_estimator import now_utc
from app.schemas.deals import AnalysisRecord, AnalysisRequest, BatchFailure, BatchResult, DealState
from app.schemas.pricing import Confidence, EstimateStatus, utc_timestamp

logger = logging.getLogger(__name__)


class AIReviewer:
    def __init__(
        self,
        config: AIConfig | None = None,
        *,
        api_key: str | None = None,
        client: ReviewClient | None = None,
        budget: AIBudget | None = None,
        clock: Callable[[], datetime] = now_utc,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config if config is not None else AIConfig()
        self._api_key = api_key
        self.client = client
        self.budget = budget
        self.clock = clock
        self.sleep = sleep
        self._owned_client: OpenAIReviewClient | None = None

    @classmethod
    def from_env(cls) -> "AIReviewer":
        return cls(AIConfig.from_env(), api_key=os.environ.get("OPENAI_API_KEY"))

    def close(self) -> None:
        if self._owned_client is not None:
            self._owned_client.close()

    def _skip_reason(self, record: AnalysisRecord) -> str | None:
        if not self.config.enabled:
            return "disabled"
        if record.state != DealState.AI_PENDING:
            return "deterministic_gate_not_passed"
        if record.pre_filters is None or record.post_filters is None or record.risk is None:
            return "incomplete_deterministic_analysis"
        if (
            record.pre_filters.rejected
            or record.pre_filters.unknown
            or record.post_filters.rejected
            or record.post_filters.unknown
            or record.risk.requires_review
        ):
            return "deterministic_gate_not_passed"
        if record.score is None or record.score.total < self.config.min_score:
            return "score_below_ai_threshold"
        estimate = record.estimate
        if (
            estimate is None
            or estimate.status != EstimateStatus.OK
            or estimate.estimated_price is None
            or estimate.confidence not in {Confidence.MEDIUM, Confidence.HIGH}
            or estimate.confidence_score < self.config.min_valuation_confidence
            or estimate.sample_size < self.config.min_comparisons
        ):
            return "unreliable_valuation"
        if record.request.snapshot.offer.is_demo and not self.config.allow_demo:
            return "demo_offline"
        return None

    def _outcome(self, review: StoredReview, *, cached: bool) -> ReviewOutcome:
        manual = (
            review.result.manual_review_required
            or review.result.confidence < self.config.min_review_confidence
            or review.result.listing_consistency < self.config.min_listing_consistency
        )
        return ReviewOutcome(
            status="cached" if cached else "completed",
            reason="ai_review_completed",
            review=review,
            manual_review_required=manual,
        )

    def review(self, record: AnalysisRecord) -> ReviewOutcome:
        if record.ai_review is not None:
            return record.ai_review
        reason = self._skip_reason(record)
        if reason is not None:
            return ReviewOutcome(status="skipped", reason=reason)
        try:
            return self._review(record)
        except Exception as exc:
            # Unknown accounting/client failures cannot authorize an unmetered call.
            logger.error(
                "AI unavailable analysis=%s error=%s", record.analysis_id, type(exc).__name__
            )
            return ReviewOutcome(status="unavailable", reason="ai_service_unavailable")

    def _review(self, record: AnalysisRecord) -> ReviewOutcome:
        payload = review_payload(record)
        key = input_hash(payload, self.config.model)
        budget = self.budget
        if budget is None and self.config.db_path.exists():
            budget = self.budget = AIBudget(self.config)
        if budget is not None:
            if budget.config != self.config:
                raise ValueError("budget and reviewer must use the same policy")
            cached = budget.get_cached(key)
            if cached is not None:
                return self._outcome(cached, cached=True)
        if not self._api_key or not self._api_key.strip():
            return ReviewOutcome(status="skipped", reason="missing_api_key")
        if not self.config.pricing_configured:
            return ReviewOutcome(status="skipped", reason="pricing_unconfigured")
        if input_token_upper_bound(payload) > self.config.max_input_tokens:
            return ReviewOutcome(status="skipped", reason="input_too_large")
        if budget is None:
            budget = self.budget = AIBudget(self.config)
        claim = budget.claim(key, self.config.model, PROMPT_VERSION, self.clock())
        if claim.status == "cached":
            assert claim.review is not None
            return self._outcome(claim.review, cached=True)
        if claim.status == "pending":
            return ReviewOutcome(status="pending", reason="review_in_progress")
        if claim.status != "reserved":
            return ReviewOutcome(status="unavailable", reason=claim.status)
        assert claim.attempt_id is not None
        attempt_id = claim.attempt_id
        if self.client is None:
            assert self._api_key is not None
            self._owned_client = OpenAIReviewClient(self._api_key, self.config)
            self.client = self._owned_client
        for attempt in range(self.config.max_attempts):
            try:
                result = self.client.review(payload)
            except ReviewClientError as exc:
                budget.settle(attempt_id, None)
                logger.warning(
                    "AI attempt failed hash=%s model=%s prompt=%s error=%s",
                    key,
                    self.config.model,
                    PROMPT_VERSION,
                    exc.code,
                )
                delay = max(
                    exc.retry_after,
                    min(self.config.max_backoff_seconds, self.config.backoff_seconds * 2**attempt),
                )
                if (
                    not exc.transient
                    or attempt + 1 >= self.config.max_attempts
                    or delay > self.config.max_backoff_seconds
                ):
                    budget.finish(key, None)
                    return ReviewOutcome(status="unavailable", reason=exc.code)
                self.sleep(delay)
                next_attempt = budget.reserve_retry(key, self.clock())
                if next_attempt is None:
                    budget.finish(key, None)
                    return ReviewOutcome(status="unavailable", reason="retry_budget_exhausted")
                attempt_id = next_attempt
                continue
            if result.review is None:
                budget.settle(attempt_id, result.usage)
                budget.finish(key, None)
                return ReviewOutcome(status="unavailable", reason=result.reason)
            validated = AIReview.model_validate(result.review.model_dump())
            try:
                cost = budget.settle(attempt_id, result.usage)
            except BudgetUnavailable:
                cost = None
                logger.error("AI accounting write failed hash=%s", key)
            stored = StoredReview(
                input_hash=key,
                model=result.model,
                prompt_version=PROMPT_VERSION,
                reviewed_at=utc_timestamp(self.clock()),
                result=validated,
                usage=result.usage,
                cost_usd=cost,
                reserved_usd=budget.reservation_usd,
            )
            try:
                budget.finish(key, stored)
            except BudgetUnavailable:
                # Preserve observed high risk even when the cache write fails.
                logger.error("AI cache write failed hash=%s", key)
            logger.info(
                "AI completed hash=%s model=%s prompt=%s", key, result.model, PROMPT_VERSION
            )
            return self._outcome(stored, cached=False)
        raise AssertionError("bounded retry loop must return")


class ReviewedDealPipeline:
    """Opt-in orchestration; the deterministic core itself never performs network calls."""

    def __init__(self, pipeline: DealPipeline, reviewer: AIReviewer) -> None:
        if not pipeline.config.ai_review_required:
            raise ValueError("review orchestration requires ai_review_required=true")
        self.pipeline = pipeline
        self.reviewer = reviewer

    def process(self, request: AnalysisRequest) -> AnalysisRecord:
        record = self.pipeline.process(request)
        if record.state != DealState.AI_PENDING:
            return record
        outcome = self.reviewer.review(record)
        if outcome.status == "pending":
            return record  # Another worker owns the call; do not race its decision.
        return self.pipeline.complete_ai_review(record.analysis_id, outcome)

    def process_batch(self, requests: Iterable[AnalysisRequest]) -> BatchResult:
        records: list[AnalysisRecord] = []
        failures: list[BatchFailure] = []
        for index, request in enumerate(requests):
            try:
                records.append(self.process(request))
            except Exception as exc:
                logger.error("Reviewed batch failed index=%d error=%s", index, type(exc).__name__)
                failures.append(BatchFailure(index=index, error_type=type(exc).__name__))
        return BatchResult(records=tuple(records), failures=tuple(failures))
