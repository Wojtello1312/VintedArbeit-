"""Chronological, point-in-time demo backtest. Outcomes are read only after decisions."""

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, field_validator, model_validator

from app.config import PricingConfig
from app.core.deal_pipeline import DealPipeline
from app.deal_config import DealConfig
from app.schemas.deals import (
    AnalysisRequest,
    AvailableObservation,
    CostBreakdown,
    DealSnapshot,
    DealState,
    required_timestamp,
)
from app.schemas.pricing import FrozenSchema
from app.storage.memory import MemoryAnalysisStore
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.models import CURRENCY_SCALE, OfferInput


class RealizedOutcome(FrozenSchema):
    source_name: str
    external_id: str
    sale_price: Decimal
    currency: str
    costs: CostBreakdown
    sold_at: datetime
    available_at: datetime
    evidence_reference: str = Field(min_length=1)
    is_demo: bool = Field(strict=True)

    @field_validator("sale_price", mode="before")
    @classmethod
    def parse_price(cls, value: object) -> Decimal:
        return OfferInput.normalize_price(value)

    @field_validator("currency", mode="before")
    @classmethod
    def parse_currency(cls, value: object) -> str:
        return OfferInput.normalize_currency(value)

    @field_validator("sold_at", "available_at", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime:
        return required_timestamp(value)

    @model_validator(mode="after")
    def coherent_outcome(self) -> Self:
        if self.available_at < self.sold_at or self.costs.currency != self.currency:
            raise ValueError("incoherent outcome time or currency")
        if self.sale_price != self.sale_price.quantize(
            Decimal(1).scaleb(-CURRENCY_SCALE[self.currency])
        ):
            raise ValueError("sale price precision exceeds currency scale")
        if not self.evidence_reference.strip():
            raise ValueError("outcome requires a sale evidence reference")
        return self


class HistoricalDemo(FrozenSchema):
    description: str
    evaluation_at: datetime
    snapshots: tuple[DealSnapshot, ...]
    observations: tuple[AvailableObservation, ...]
    outcomes: tuple[RealizedOutcome, ...] = ()

    @field_validator("evaluation_at", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime:
        return required_timestamp(value)

    @model_validator(mode="after")
    def demo_only(self) -> Self:
        identities = [
            (snapshot.offer.source_name, snapshot.offer.external_id) for snapshot in self.snapshots
        ]
        outcomes = [(item.source_name, item.external_id) for item in self.outcomes]
        if len(set(identities)) != len(identities) or len(set(outcomes)) != len(outcomes):
            raise ValueError("backtest requires unique target identities and outcomes")
        if (
            any(not snapshot.offer.is_demo for snapshot in self.snapshots)
            or any(not item.observation.is_demo for item in self.observations)
            or any(not item.is_demo for item in self.outcomes)
        ):
            raise ValueError("this backtest only accepts explicitly marked demo data")
        return self


class BacktestRow(FrozenSchema):
    analysis_id: str
    source_name: str
    external_id: str
    as_of: datetime
    state: DealState
    score: Decimal | None
    sample_size: int
    estimated_profit: Decimal | None
    realized_profit: Decimal | None
    reasons: tuple[str, ...]


class BacktestReport(FrozenSchema):
    is_demo: bool = True
    currency: str
    evaluation_at: datetime
    decisions: dict[str, int]
    approved_with_known_outcome: int
    approved_with_unknown_outcome: int
    realized_profit_of_evaluated_approvals: Decimal | None
    rows: tuple[BacktestRow, ...]


def run_backtest(
    dataset: HistoricalDemo,
    config: DealConfig | None = None,
    pricing_config: PricingConfig | None = None,
) -> BacktestReport:
    policy = config if config is not None else DealConfig()
    pipeline = DealPipeline(MemoryAnalysisStore(), policy, pricing_config)
    snapshots = sorted(
        (item for item in dataset.snapshots if item.available_at <= dataset.evaluation_at),
        key=lambda item: (item.available_at, item.offer.source_name, item.offer.external_id),
    )
    requests = [
        AnalysisRequest(
            snapshot=snapshot,
            observations=tuple(
                item
                for item in dataset.observations
                if item.available_at <= snapshot.available_at
                and item.observation.observed_at <= snapshot.available_at
            ),
            as_of=snapshot.available_at,
        )
        for snapshot in snapshots
    ]
    decisions = pipeline.process_batch(requests)
    if decisions.failures:
        raise RuntimeError("backtest could not complete all analysis records")
    # No future labels are passed to the engine. Evaluate them only after every decision is fixed.
    known_outcomes = {
        (item.source_name, item.external_id): item
        for item in dataset.outcomes
        if item.available_at <= dataset.evaluation_at
    }
    rows: list[BacktestRow] = []
    profits: list[Decimal] = []
    approved = 0
    for record in decisions.records:
        offer = record.request.snapshot.offer
        outcome = known_outcomes.get((offer.source_name, offer.external_id))
        realized = None
        if (
            outcome is not None
            and outcome.sold_at > record.request.as_of
            and outcome.currency == offer.currency
        ):
            costs = outcome.costs.total
            if costs is not None:
                realized = outcome.sale_price - offer.asking_price - costs
        if record.state == DealState.APPROVED:
            approved += 1
            if realized is not None:
                profits.append(realized)
        rows.append(
            BacktestRow(
                analysis_id=record.analysis_id,
                source_name=offer.source_name,
                external_id=offer.external_id,
                as_of=record.request.as_of,
                state=record.state,
                score=None if record.score is None else record.score.total,
                sample_size=0 if record.estimate is None else record.estimate.sample_size,
                estimated_profit=None if record.profit is None else record.profit.estimated_profit,
                realized_profit=realized,
                reasons=tuple(reason.code for reason in record.reasons),
            )
        )
    return BacktestReport(
        currency=policy.currency,
        evaluation_at=dataset.evaluation_at,
        decisions=dict(sorted(Counter(row.state.value for row in rows).items())),
        approved_with_known_outcome=len(profits),
        approved_with_unknown_outcome=approved - len(profits),
        realized_profit_of_evaluated_approvals=sum(profits, Decimal(0)) if profits else None,
        rows=tuple(rows),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Historical demo backtest without future information"
    )
    parser.add_argument("--data", type=Path, help="Explicitly marked historical demo JSON")
    args = parser.parse_args(argv)
    try:
        data = (
            args.data.read_bytes()
            if args.data
            else files("app").joinpath("data/backtest_demo.json").read_bytes()
        )
        dataset = HistoricalDemo.model_validate_json(data)
        settings = Settings.from_env()
        report = run_backtest(dataset, settings.deal, settings.pricing)
    except (OSError, ValidationError, RuntimeError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": "Backtest input or processing failed"}
            )
        )
        return 2
    print(report.model_dump_json(indent=2))
    return 1 if report.decisions.get(DealState.ERROR.value) else 0
