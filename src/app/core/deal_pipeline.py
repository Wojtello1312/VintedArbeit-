"""Deterministic state machine with atomic checkpoints and no external side effects."""

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal

from app.ai.schemas import ReviewOutcome
from app.config import PricingConfig
from app.core.filters import post_filters, pre_filters
from app.core.price_estimator import PriceEstimator, now_utc
from app.core.product_matcher import ProductMatcher
from app.core.risk_engine import assess_risk
from app.core.scoring import calculate_profit, score_deal
from app.deal_config import PIPELINE_VERSION, SCORING_VERSION, DealConfig
from app.ports import AnalysisStore, StoreUnavailable
from app.schemas.deals import (
    AnalysisRecord,
    AnalysisRequest,
    BatchFailure,
    BatchResult,
    CandidateMatch,
    DealState,
    DecisionReason,
    NotificationReceipt,
    RiskAssessment,
    RiskFlag,
    RiskLevel,
    StateTransition,
    required_timestamp,
)
from app.schemas.pricing import EstimateStatus, MarketEstimate, PriceObservation, PricingProduct

logger = logging.getLogger(__name__)
TERMINAL = {
    DealState.APPROVED,
    DealState.REJECTED,
    DealState.REVIEW_REQUIRED,
    DealState.NOTIFIED,
    DealState.INSUFFICIENT_DATA,
    DealState.AI_PENDING,
}
TRANSITIONS = {
    DealState.NEW: {DealState.NORMALIZED},
    DealState.NORMALIZED: {DealState.FILTERED},
    DealState.FILTERED: {DealState.MATCHED, DealState.REJECTED, DealState.REVIEW_REQUIRED},
    DealState.MATCHED: {DealState.VALUED},
    DealState.VALUED: {DealState.SCORED, DealState.INSUFFICIENT_DATA},
    DealState.SCORED: {
        DealState.APPROVED,
        DealState.REJECTED,
        DealState.REVIEW_REQUIRED,
        DealState.AI_PENDING,
    },
    DealState.APPROVED: {DealState.NOTIFIED},
    DealState.AI_PENDING: {DealState.APPROVED, DealState.REVIEW_REQUIRED},
}


def visible_observations(request: AnalysisRequest) -> tuple[PriceObservation, ...]:
    """Latest known snapshot per listing; conflicting ties remain visible for rejection."""
    latest: dict[tuple[str, str], datetime] = {}
    for item in request.observations:
        if item.available_at <= request.as_of and item.observation.observed_at <= request.as_of:
            key = (item.observation.source_name, item.observation.external_id)
            latest[key] = max(latest.get(key, item.available_at), item.available_at)
    return tuple(
        item.observation
        for item in request.observations
        if item.available_at
        == latest.get((item.observation.source_name, item.observation.external_id))
        and item.observation.observed_at <= request.as_of
    )


class DealPipeline:
    def __init__(
        self,
        store: AnalysisStore,
        config: DealConfig | None = None,
        pricing_config: PricingConfig | None = None,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = store
        self.config = config if config is not None else DealConfig()
        pricing = pricing_config if pricing_config is not None else PricingConfig()
        self.pricing_config = PricingConfig.model_validate(
            {
                **pricing.model_dump(),
                "match_threshold": max(pricing.match_threshold, float(self.config.min_match_score)),
            }
        )
        self.clock = clock
        self.policy_json = json.dumps(
            {
                "deal": self.config.model_dump(mode="json"),
                "pricing": self.pricing_config.model_dump(mode="json"),
                "pipeline_version": PIPELINE_VERSION,
                "scoring_version": SCORING_VERSION,
            },
            sort_keys=True,
        )

    def analysis_id(self, request: AnalysisRequest) -> str:
        data = request.model_dump(mode="json", exclude={"observations"})
        data["observations"] = sorted(item.model_dump_json() for item in request.observations)
        return hashlib.sha256(
            (self.policy_json + json.dumps(data, sort_keys=True)).encode()
        ).hexdigest()

    def process(self, request: AnalysisRequest) -> AnalysisRecord:
        identity = self.analysis_id(request)
        record = self.store.create(
            AnalysisRecord(
                analysis_id=identity,
                request=request,
                policy_json=self.policy_json,
                scoring_version=SCORING_VERSION,
            )
        )
        while record.state not in TERMINAL:
            if record.state == DealState.ERROR:
                if record.attempts >= self.config.max_attempts:
                    return record
                assert record.resume_state is not None
                next_record = self._transition(
                    record,
                    record.resume_state,
                    attempts=record.attempts + 1,
                    reasons=(
                        DecisionReason(code="retry", message="Resuming the last committed stage"),
                    ),
                )
            else:
                try:
                    next_record = self._advance(record)
                except Exception as exc:
                    logger.error(
                        "Deal stage failed analysis=%s stage=%s error=%s",
                        identity,
                        record.state,
                        type(exc).__name__,
                    )
                    next_record = self._transition(
                        record,
                        DealState.ERROR,
                        resume_state=record.state,
                        attempts=max(1, record.attempts),
                        reasons=(
                            DecisionReason(
                                code="stage_error", message=f"Stage failed: {type(exc).__name__}"
                            ),
                        ),
                    )
            if self.store.compare_and_set(next_record, record.revision):
                record = next_record
                if record.state == DealState.ERROR:
                    return record
            else:
                latest = self.store.get(identity)
                if latest is None:
                    raise StoreUnavailable("checkpoint disappeared")
                record = latest
        return record

    def process_batch(self, requests: Iterable[AnalysisRequest]) -> BatchResult:
        records: list[AnalysisRecord] = []
        failures: list[BatchFailure] = []
        for index, request in enumerate(requests):
            try:
                records.append(self.process(request))
            except Exception as exc:
                logger.error(
                    "Batch item could not be checkpointed index=%d error=%s",
                    index,
                    type(exc).__name__,
                )
                failures.append(BatchFailure(index=index, error_type=type(exc).__name__))
        return BatchResult(records=tuple(records), failures=tuple(failures))

    def _transition(
        self,
        record: AnalysisRecord,
        state: DealState,
        *,
        reasons: tuple[DecisionReason, ...] = (),
        **changes: object,
    ) -> AnalysisRecord:
        if record.state == DealState.ERROR:
            allowed = state == record.resume_state
        else:
            allowed = state in TRANSITIONS.get(record.state, set()) or (
                state == DealState.ERROR and record.state not in TERMINAL
            )
        if not allowed:
            raise ValueError(f"invalid state transition {record.state} -> {state}")
        transition = StateTransition(
            previous=record.state,
            current=state,
            recorded_at=required_timestamp(self.clock()),
            reasons=reasons,
        )
        return AnalysisRecord.model_validate(
            {
                **record.model_dump(),
                **changes,
                "state": state,
                "revision": record.revision + 1,
                "history": (*record.history, transition),
                "reasons": reasons,
            }
        )

    def _value(self, request: AnalysisRequest) -> MarketEstimate:
        target = PricingProduct.from_offer(request.snapshot.offer, model=request.snapshot.model)
        return PriceEstimator(self.pricing_config, clock=lambda: request.as_of).estimate(
            target,
            visible_observations(request),
        )

    def _advance(self, record: AnalysisRecord) -> AnalysisRecord:
        request = record.request
        snapshot = request.snapshot
        match record.state:
            case DealState.NEW:
                return self._transition(
                    record, DealState.NORMALIZED, attempts=max(1, record.attempts)
                )
            case DealState.NORMALIZED:
                return self._transition(
                    record, DealState.FILTERED, pre_filters=pre_filters(snapshot, self.config)
                )
            case DealState.FILTERED:
                assert record.pre_filters is not None
                if record.pre_filters.rejected:
                    return self._transition(
                        record, DealState.REJECTED, reasons=record.pre_filters.rejected
                    )
                if record.pre_filters.unknown:
                    return self._transition(
                        record, DealState.REVIEW_REQUIRED, reasons=record.pre_filters.unknown
                    )
                matcher = ProductMatcher(self.pricing_config)
                target = PricingProduct.from_offer(snapshot.offer, model=snapshot.model)
                matches = tuple(
                    CandidateMatch(
                        source_name=item.source_name,
                        external_id=item.external_id,
                        result=matcher.match(target.features, item.features),
                    )
                    for item in visible_observations(request)
                )
                return self._transition(record, DealState.MATCHED, matches=matches)
            case DealState.MATCHED:
                return self._transition(record, DealState.VALUED, estimate=self._value(request))
            case DealState.VALUED:
                assert record.estimate is not None
                estimate = record.estimate
                if (
                    estimate.status == EstimateStatus.INSUFFICIENT_DATA
                    or estimate.sample_size < self.config.min_comparisons
                ):
                    return self._transition(
                        record,
                        DealState.INSUFFICIENT_DATA,
                        reasons=(
                            DecisionReason(
                                code="insufficient_comparisons",
                                message="Too few eligible price observations after filtering",
                            ),
                        ),
                    )
                used = {
                    (item.source_name, item.external_id) for item in estimate.included_observations
                }
                scores = [
                    Decimal(str(item.result.product_match_score))
                    for item in record.matches
                    if (item.source_name, item.external_id) in used
                ]
                profit = calculate_profit(snapshot, estimate, self.config)
                return self._transition(
                    record,
                    DealState.SCORED,
                    profit=profit,
                    post_filters=post_filters(
                        estimate, profit, min(scores) if scores else None, self.config
                    ),
                    score=score_deal(snapshot, estimate, profit, request.as_of, self.config),
                    risk=assess_risk(snapshot, estimate, profit, request.as_of, self.config),
                )
            case DealState.SCORED:
                assert (
                    record.post_filters is not None
                    and record.risk is not None
                    and record.score is not None
                )
                if record.post_filters.rejected:
                    return self._transition(
                        record, DealState.REJECTED, reasons=record.post_filters.rejected
                    )
                review_reasons = record.post_filters.unknown + tuple(
                    DecisionReason(code=flag.code, message=flag.message)
                    for flag in record.risk.flags
                    if flag.requires_review
                )
                if review_reasons:
                    return self._transition(
                        record, DealState.REVIEW_REQUIRED, reasons=review_reasons
                    )
                if record.score.total < self.config.approval_score:
                    return self._transition(
                        record,
                        DealState.REJECTED,
                        reasons=(
                            DecisionReason(
                                code="score_below_minimum",
                                message="Weighted score below approval threshold",
                            ),
                        ),
                    )
                if self.config.ai_review_required:
                    return self._transition(
                        record,
                        DealState.AI_PENDING,
                        reasons=(
                            DecisionReason(
                                code="ai_review_pending",
                                message="Waiting for optional review; no AI call has been made",
                            ),
                        ),
                    )
                return self._transition(
                    record,
                    DealState.APPROVED,
                    reasons=(
                        DecisionReason(
                            code="policy_passed",
                            message="All deterministic approval criteria passed",
                        ),
                    ),
                )
            case _:
                raise ValueError("unexpected nonterminal state")

    def complete_ai_review(self, analysis_id: str, outcome: ReviewOutcome) -> AnalysisRecord:
        """Persist an optional review without changing prices, scores or lowering known risk."""
        outcome = ReviewOutcome.model_validate(outcome.model_dump())
        if outcome.status == "pending":
            raise ValueError("cannot complete an active review")
        while True:
            record = self.store.get(analysis_id)
            if record is None:
                raise ValueError("unknown analysis")
            if record.ai_review is not None:
                return record
            if record.state != DealState.AI_PENDING or record.risk is None:
                raise ValueError("only deterministically qualified AI_PENDING records may complete")
            risk = record.risk
            flags = risk.flags
            level = risk.level
            if outcome.review is not None:
                ai_level = outcome.review.result.risk_level
                proposed = {
                    "low": RiskLevel.LOW,
                    "medium": RiskLevel.MEDIUM,
                    "unknown": RiskLevel.MEDIUM,
                    "high": RiskLevel.HIGH,
                }[ai_level]
                rank = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
                level = max((level, proposed), key=rank.__getitem__)
                if proposed != RiskLevel.LOW or outcome.manual_review_required:
                    flags = (
                        *flags,
                        RiskFlag(
                            code="ai_review_findings",
                            message="See the recorded AI review and limitations",
                            level=proposed,
                            requires_review=outcome.manual_review_required,
                        ),
                    )
            manual = risk.requires_review or outcome.manual_review_required
            updated = self._transition(
                record,
                DealState.REVIEW_REQUIRED if manual else DealState.APPROVED,
                ai_review=outcome,
                risk=RiskAssessment(level=level, flags=flags, requires_review=manual),
                reasons=(
                    DecisionReason(
                        code=outcome.reason,
                        message=(
                            "Optional AI review recorded"
                            if outcome.review is not None
                            else "AI unavailable or skipped; deterministic-only decision"
                        ),
                    ),
                ),
            )
            if self.store.compare_and_set(updated, record.revision):
                return updated

    def mark_notified(self, analysis_id: str, receipt: NotificationReceipt) -> AnalysisRecord:
        """Record a receipt from a future delivery adapter; this method never sends a message."""
        while True:
            record = self.store.get(analysis_id)
            if record is None:
                raise ValueError("unknown analysis")
            if record.state == DealState.NOTIFIED:
                if record.notification_receipt != receipt:
                    raise ValueError("analysis already has a different receipt")
                return record
            if record.state != DealState.APPROVED:
                raise ValueError("only approved analyses can be marked notified")
            if record.request.snapshot.offer.is_demo and receipt.channel != "demo":
                raise ValueError("demo analyses require a demo receipt")
            if receipt.delivered_at < record.history[-1].recorded_at:
                raise ValueError("delivery cannot precede approval")
            if receipt.delivered_at > required_timestamp(self.clock()):
                raise ValueError("delivery cannot be confirmed in the future")
            updated = self._transition(
                record,
                DealState.NOTIFIED,
                notification_receipt=receipt,
                reasons=(
                    DecisionReason(code="delivery_confirmed", message="Delivery receipt recorded"),
                ),
            )
            if self.store.compare_and_set(updated, record.revision):
                return updated
