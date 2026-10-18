from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.deal_pipeline import DealPipeline
from app.deal_config import DealConfig
from app.ports import StoreUnavailable
from app.schemas.deals import (
    AnalysisRecord,
    AnalysisRequest,
    DealState,
    NotificationReceipt,
)
from app.schemas.pricing import MarketEstimate
from app.storage.memory import MemoryAnalysisStore
from app.storage.sqlite import SQLiteAnalysisStore


def updated(request: AnalysisRequest, **snapshot_updates: object) -> AnalysisRequest:
    return AnalysisRequest.model_validate(
        {
            **request.model_dump(),
            "snapshot": {
                **request.snapshot.model_dump(),
                **snapshot_updates,
            },
        }
    )


def test_full_pipeline_and_idempotence(request_data: AnalysisRequest, deal_now: datetime) -> None:
    pipeline = DealPipeline(MemoryAnalysisStore(), clock=lambda: deal_now)
    first = pipeline.process(request_data)
    assert first.state == DealState.APPROVED and first.attempts == 1
    assert [item.current for item in first.history] == [
        DealState.NORMALIZED,
        DealState.FILTERED,
        DealState.MATCHED,
        DealState.VALUED,
        DealState.SCORED,
        DealState.APPROVED,
    ]
    assert first.profit.estimated_profit == Decimal(130)
    assert first.score.total > 70 and first.reasons[0].code == "policy_passed"
    repeated = pipeline.process(request_data)
    assert repeated == first
    assert (
        pipeline.process(
            request_data.model_copy(
                update={"observations": tuple(reversed(request_data.observations))}
            )
        )
        == first
    )


def test_unknown_costs_cannot_be_approved(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    request = updated(
        request_data, costs={**request_data.snapshot.costs.model_dump(), "resale_fees": None}
    )
    result = DealPipeline(MemoryAnalysisStore(), clock=lambda: deal_now).process(request)
    assert result.state == DealState.REVIEW_REQUIRED
    assert result.profit.estimated_profit is None
    assert {reason.code for reason in result.reasons} >= {"profit_unknown", "unknown_costs"}


def test_insufficient_and_unknown_category_are_distinct(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    pipeline = DealPipeline(
        MemoryAnalysisStore(), DealConfig(allowed_categories=("sneakers",)), clock=lambda: deal_now
    )
    unknown = pipeline.process(updated(request_data, category=None))
    assert unknown.state == DealState.REVIEW_REQUIRED
    insufficient = pipeline.process(request_data.model_copy(update={"observations": ()}))
    assert insufficient.state == DealState.INSUFFICIENT_DATA
    assert insufficient.estimate.estimated_price is None
    assert insufficient.score is None


def test_approval_threshold_boundary(request_data: AnalysisRequest, deal_now: datetime) -> None:
    base = DealPipeline(MemoryAnalysisStore(), clock=lambda: deal_now).process(request_data)
    threshold = base.score.total
    at = DealPipeline(
        MemoryAnalysisStore(), DealConfig(approval_score=threshold), clock=lambda: deal_now
    ).process(request_data)
    above = DealPipeline(
        MemoryAnalysisStore(),
        DealConfig(approval_score=threshold + Decimal("0.0001")),
        clock=lambda: deal_now,
    ).process(request_data)
    assert at.state == DealState.APPROVED and above.state == DealState.REJECTED
    assert at.analysis_id != above.analysis_id


def test_stricter_match_and_comparison_minima(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    too_few = DealPipeline(
        MemoryAnalysisStore(), DealConfig(min_comparisons=21), clock=lambda: deal_now
    ).process(request_data)
    assert too_few.state == DealState.INSUFFICIENT_DATA
    missing_model = updated(request_data, model=None)
    missing_model = AnalysisRequest.model_validate(
        {
            **missing_model.model_dump(),
            "observations": [
                {
                    **item.model_dump(),
                    "observation": {
                        **item.observation.model_dump(),
                        "features": {
                            **item.observation.features.model_dump(),
                            "model": None,
                        },
                    },
                }
                for item in missing_model.observations
            ],
        }
    )
    stricter = DealPipeline(
        MemoryAnalysisStore(), DealConfig(min_match_score=Decimal(81)), clock=lambda: deal_now
    ).process(missing_model)
    assert stricter.state == DealState.INSUFFICIENT_DATA
    assert all(not item.result.accepted for item in stricter.matches)


def test_ai_pending_does_not_call_a_service(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    pipeline = DealPipeline(
        MemoryAnalysisStore(), DealConfig(ai_review_required=True), clock=lambda: deal_now
    )
    record = pipeline.process(request_data)
    assert record.state == DealState.AI_PENDING
    assert record.reasons[0].code == "ai_review_pending"
    assert pipeline.process(request_data) == record


@pytest.mark.parametrize(
    "interrupted_at",
    [
        DealState.NORMALIZED,
        DealState.FILTERED,
        DealState.MATCHED,
        DealState.VALUED,
        DealState.SCORED,
    ],
)
def test_restart_from_every_committed_stage(
    request_data: AnalysisRequest,
    deal_now: datetime,
    tmp_path: Path,
    interrupted_at: DealState,
) -> None:
    class InterruptedStore(SQLiteAnalysisStore):
        def compare_and_set(self, record: AnalysisRecord, expected_revision: int) -> bool:
            committed = super().compare_and_set(record, expected_revision)
            if committed and record.state == interrupted_at:
                raise KeyboardInterrupt("simulated process termination")
            return committed

    path = tmp_path / "checkpoint.db"
    store = InterruptedStore(path)
    pipeline = DealPipeline(store, clock=lambda: deal_now)
    with pytest.raises(KeyboardInterrupt):
        pipeline.process(request_data)
    assert store.get(pipeline.analysis_id(request_data)).state == interrupted_at
    store.close()
    recovered = SQLiteAnalysisStore(path)
    try:
        result = DealPipeline(recovered, clock=lambda: deal_now).process(request_data)
        assert result.state == DealState.APPROVED and result.revision == 6
        assert len(result.history) == 6 and result.attempts == 1
        assert len(set(item.current for item in result.history)) == 6
    finally:
        recovered.close()


def test_error_can_be_retried_without_repeating_earlier_stages(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    class FlakyPipeline(DealPipeline):
        calls = 0

        def _value(self, request: AnalysisRequest) -> MarketEstimate:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("sensitive error details must not be persisted")
            return super()._value(request)

    pipeline = FlakyPipeline(MemoryAnalysisStore(), clock=lambda: deal_now)
    error = pipeline.process(request_data)
    assert error.state == DealState.ERROR and error.resume_state == DealState.MATCHED
    assert "sensitive error" not in error.model_dump_json()
    result = pipeline.process(request_data)
    assert result.state == DealState.APPROVED and result.attempts == 2
    assert sum(item.current == DealState.FILTERED for item in result.history) == 1
    assert any(item.current == DealState.ERROR for item in result.history)


def test_retry_budget_is_enforced(request_data: AnalysisRequest, deal_now: datetime) -> None:
    class BrokenPipeline(DealPipeline):
        calls = 0

        def _value(self, request: AnalysisRequest) -> MarketEstimate:
            self.calls += 1
            raise RuntimeError("always fails")

    pipeline = BrokenPipeline(
        MemoryAnalysisStore(), DealConfig(max_attempts=2), clock=lambda: deal_now
    )
    assert pipeline.process(request_data).state == DealState.ERROR
    second = pipeline.process(request_data)
    assert second.attempts == 2
    assert pipeline.process(request_data) == second and pipeline.calls == 2


def test_batch_continues_after_a_checkpoint_failure(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    class FailingStore(MemoryAnalysisStore):
        def create(self, record: AnalysisRecord) -> AnalysisRecord:
            if record.request.snapshot.offer.external_id == "DEMO-broken":
                raise StoreUnavailable("database unavailable")
            return super().create(record)

    broken = updated(
        request_data,
        offer={**request_data.snapshot.offer.model_dump(), "external_id": "DEMO-broken"},
    )
    batch = DealPipeline(FailingStore(), clock=lambda: deal_now).process_batch(
        [broken, request_data]
    )
    assert len(batch.failures) == 1 and batch.failures[0].index == 0
    assert batch.records[0].state == DealState.APPROVED


def test_uncommitted_checkpoint_is_recomputed(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    class FailingCommit(MemoryAnalysisStore):
        broken = True

        def compare_and_set(self, record: AnalysisRecord, expected_revision: int) -> bool:
            if self.broken and record.state == DealState.MATCHED:
                raise StoreUnavailable("commit failed")
            return super().compare_and_set(record, expected_revision)

    store = FailingCommit()
    pipeline = DealPipeline(store, clock=lambda: deal_now)
    with pytest.raises(StoreUnavailable):
        pipeline.process(request_data)
    assert store.get(pipeline.analysis_id(request_data)).state == DealState.FILTERED
    store.broken = False
    assert pipeline.process(request_data).state == DealState.APPROVED


def test_concurrent_workers_commit_one_history(
    request_data: AnalysisRequest, deal_now: datetime, tmp_path: Path
) -> None:
    stores = [SQLiteAnalysisStore(tmp_path / "shared.db") for _ in range(2)]
    try:
        pipelines = [DealPipeline(store, clock=lambda: deal_now) for store in stores]
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(lambda pipeline: pipeline.process(request_data), pipelines))
        assert records[0] == records[1]
        assert records[0].revision == 6
    finally:
        for store in stores:
            store.close()


def test_changed_inputs_produce_new_versioned_analysis(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    store = MemoryAnalysisStore()
    first = DealPipeline(store, clock=lambda: deal_now).process(request_data)
    updated_cost = updated(
        request_data, costs={**request_data.snapshot.costs.model_dump(), "other_costs": "5"}
    )
    second = DealPipeline(store, clock=lambda: deal_now).process(updated_cost)
    assert first.analysis_id != second.analysis_id
    assert first.profit.estimated_profit - second.profit.estimated_profit == Decimal(5)
    assert "deal-score-v1" in first.policy_json and first.scoring_version == first.score.version


def test_notification_receipt_is_explicit_and_idempotent(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    pipeline = DealPipeline(MemoryAnalysisStore(), clock=lambda: deal_now)
    record = pipeline.process(request_data)
    receipt = NotificationReceipt(receipt_id="DEMO-delivery", channel="demo", delivered_at=deal_now)
    result = pipeline.mark_notified(record.analysis_id, receipt)
    assert result.state == DealState.NOTIFIED
    assert pipeline.mark_notified(record.analysis_id, receipt) == result
    assert pipeline.process(request_data) == result
    with pytest.raises(ValueError):
        pipeline.mark_notified(
            record.analysis_id, receipt.model_copy(update={"receipt_id": "another"})
        )


@pytest.mark.parametrize(
    "change",
    [
        {"channel": "telegram"},
        {"delivered_at": "2025-01-01T00:00:00Z"},
        {"delivered_at": "2027-01-01T00:00:00Z"},
    ],
)
def test_invalid_demo_receipts_do_not_change_state(
    request_data: AnalysisRequest, deal_now: datetime, change: dict[str, object]
) -> None:
    pipeline = DealPipeline(MemoryAnalysisStore(), clock=lambda: deal_now)
    record = pipeline.process(request_data)
    receipt = NotificationReceipt.model_validate(
        {"receipt_id": "DEMO", "channel": "demo", "delivered_at": deal_now, **change}
    )
    with pytest.raises(ValueError):
        pipeline.mark_notified(record.analysis_id, receipt)
    assert pipeline.process(request_data).state == DealState.APPROVED


def test_future_price_observation_is_not_used(
    request_data: AnalysisRequest, deal_now: datetime
) -> None:
    observation = request_data.observations[0]
    future = observation.model_copy(update={"available_at": deal_now + timedelta(seconds=1)})
    request = request_data.model_copy(update={"observations": (future,)})
    assert DealPipeline(MemoryAnalysisStore()).process(request).state == DealState.INSUFFICIENT_DATA


def test_later_revision_is_used_only_after_it_becomes_available(
    request_data: AnalysisRequest,
    deal_now: datetime,
) -> None:
    from app.core.deal_pipeline import visible_observations

    old = request_data.observations[0]
    revised = old.model_copy(
        update={
            "available_at": deal_now + timedelta(seconds=1),
            "observation": old.observation.model_copy(update={"price": Decimal(1000)}),
        }
    )
    request = request_data.model_copy(update={"observations": (old, revised)})
    assert visible_observations(request) == (old.observation,)
    later = request.model_copy(update={"as_of": deal_now + timedelta(seconds=1)})
    assert visible_observations(later) == (revised.observation,)
