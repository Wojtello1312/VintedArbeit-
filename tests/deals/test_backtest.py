import json
from datetime import timedelta
from decimal import Decimal
from importlib.resources import files

import pytest
from pydantic import ValidationError

from app.backtest import HistoricalDemo, main, run_backtest


def demo() -> HistoricalDemo:
    return HistoricalDemo.model_validate_json(
        files("app").joinpath("data/backtest_demo.json").read_bytes()
    )


def decision_view(report):
    return [
        (row.analysis_id, row.state, row.score, row.estimated_profit, row.sample_size)
        for row in report.rows
    ]


def test_backtest_expected_demo_results_and_reproducibility() -> None:
    first = run_backtest(demo())
    assert first.decisions == {
        "APPROVED": 4,
        "INSUFFICIENT_DATA": 2,
        "REJECTED": 3,
        "REVIEW_REQUIRED": 3,
    }
    assert first.approved_with_known_outcome == 3 and first.approved_with_unknown_outcome == 1
    assert first.realized_profit_of_evaluated_approvals == Decimal(462)
    assert run_backtest(demo()) == first


def test_future_and_delayed_observations_cannot_change_earlier_decisions() -> None:
    dataset = demo()
    last_decision = max(snapshot.available_at for snapshot in dataset.snapshots)
    poisoned = []
    for item in dataset.observations:
        if item.available_at > last_decision:
            item = item.model_copy(
                update={
                    "observation": item.observation.model_copy(update={"price": Decimal(999999)})
                }
            )
        poisoned.append(item)
    altered = dataset.model_copy(update={"observations": tuple(poisoned)})
    assert decision_view(run_backtest(altered)) == decision_view(run_backtest(dataset))


def test_future_outcomes_change_evaluation_only_not_signals() -> None:
    dataset = demo()
    altered = dataset.model_copy(
        update={
            "outcomes": tuple(
                item.model_copy(update={"sale_price": Decimal(1)}) for item in dataset.outcomes
            )
        }
    )
    baseline, changed = run_backtest(dataset), run_backtest(altered)
    assert decision_view(baseline) == decision_view(changed)
    assert (
        baseline.realized_profit_of_evaluated_approvals
        != changed.realized_profit_of_evaluated_approvals
    )
    absent = run_backtest(dataset.model_copy(update={"outcomes": ()}))
    assert absent.approved_with_unknown_outcome == 4
    assert absent.realized_profit_of_evaluated_approvals is None


def test_cutoff_excludes_unavailable_targets_and_outcomes() -> None:
    dataset = demo()
    early = dataset.snapshots[0].available_at
    result = run_backtest(dataset.model_copy(update={"evaluation_at": early}))
    assert len(result.rows) == 1
    assert result.approved_with_unknown_outcome == 1
    assert result.realized_profit_of_evaluated_approvals is None


def test_production_data_and_impossible_knowledge_times_are_rejected() -> None:
    data = demo().model_dump()
    data["snapshots"][0]["offer"]["is_demo"] = False
    with pytest.raises(ValidationError):
        HistoricalDemo.model_validate(data)
    data = demo().model_dump()
    data["observations"][0]["available_at"] = data["observations"][0]["observation"][
        "observed_at"
    ] - timedelta(seconds=1)
    with pytest.raises(ValidationError):
        HistoricalDemo.model_validate(data)


def test_backtest_cli_reports_success(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["is_demo"] is True and len(report["rows"]) == 12
