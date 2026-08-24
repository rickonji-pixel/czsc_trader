from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from czsc_trader.optuna_benchmark import (
    run_inmemory_benchmark,
    summarize_batch_timings,
)
from czsc_trader.optuna_search import TrialOutcome


PROTOCOL_PATH = Path("experiments/0824_EX07/artifacts/protocol.json")
EX04_PATH = Path("experiments/0824_EX04/artifacts/frozen_challenger.json")
CANDIDATES_PATH = Path("experiments/0824_EX06/artifacts/factor_candidates.csv")


def test_timing_summary_reports_hand_checked_phase_statistics() -> None:
    summary = summarize_batch_timings(
        [
            {
                "batch_number": 0,
                "first_trial_number": 0,
                "trial_count": 8,
                "ask_and_project_seconds": 1.0,
                "parallel_evaluate_seconds": 2.0,
                "tell_and_attrs_seconds": 1.0,
                "batch_total_seconds": 4.0,
            },
            {
                "batch_number": 1,
                "first_trial_number": 8,
                "trial_count": 8,
                "ask_and_project_seconds": 3.0,
                "parallel_evaluate_seconds": 4.0,
                "tell_and_attrs_seconds": 1.0,
                "batch_total_seconds": 8.0,
            },
        ]
    )

    assert summary["batch_count"] == 2
    assert summary["total_seconds"] == pytest.approx(12.0)
    assert summary["phases"]["ask_and_project"]["total_seconds"] == pytest.approx(4.0)
    assert summary["phases"]["ask_and_project"]["mean_seconds"] == pytest.approx(2.0)
    assert summary["phases"]["ask_and_project"]["median_seconds"] == pytest.approx(2.0)
    assert summary["phases"]["ask_and_project"]["p95_seconds"] == pytest.approx(2.9)
    assert summary["phases"]["ask_and_project"]["max_seconds"] == pytest.approx(3.0)
    assert summary["phases"]["ask_and_project"]["share"] == pytest.approx(1.0 / 3.0)
    assert summary["phases"]["parallel_evaluate"]["share"] == pytest.approx(0.5)
    assert summary["phases"]["tell_and_attrs"]["share"] == pytest.approx(1.0 / 6.0)


def _selection_context() -> SimpleNamespace:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    ex04 = json.loads(EX04_PATH.read_text(encoding="utf-8"))
    names = pd.read_csv(CANDIDATES_PATH, encoding="utf-8-sig")["factor"].tolist()
    origin = pd.Series(ex04["weights"], dtype=float).reindex(names).fillna(0.0)
    factors = pd.DataFrame(0.0, index=pd.DatetimeIndex(["2025-12-31"]), columns=names)
    return SimpleNamespace(
        protocol=protocol,
        ex04=ex04,
        origin_weights=origin,
        candidate=SimpleNamespace(factors=factors),
        data=SimpleNamespace(daily=pd.DataFrame({"dt": pd.to_datetime(["2025-12-31"])})),
        baseline=SimpleNamespace(rule=None),
        periods={},
        baseline_metrics={},
    )


def test_inmemory_benchmark_completes_exact_trials_and_publishes_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import czsc_trader.optuna_benchmark as benchmark

    monkeypatch.setattr(benchmark, "prepare_selection_inputs", lambda *args: _selection_context())

    def evaluate(requests, inputs, *, n_jobs):
        assert n_jobs == 8
        return tuple(
            TrialOutcome(
                request.number,
                0.0 if request.number == 0 else -float(request.number),
                {"active_factor_count": request.payload.active_factor_count},
            )
            for request in requests
        )

    monkeypatch.setattr(benchmark, "evaluate_trial_batch", evaluate)
    experiment_dir = tmp_path / "experiment"
    (experiment_dir / "artifacts").mkdir(parents=True)
    (experiment_dir / "artifacts" / "protocol.json").write_text(
        PROTOCOL_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    output_root = tmp_path / "outputs" / "benchmarks"

    result = run_inmemory_benchmark(
        tmp_path / "raw",
        tmp_path / "baselines",
        experiment_dir,
        output_root,
        completed_trials=16,
    )

    assert result["benchmark_kind"] == "engineering_only_ex07_inmemory"
    assert result["completed_trials"] == 16
    assert result["failed_trials"] == 0
    assert result["selection_sample_end"] == "2025-12-31"
    assert result["trial_zero_objective"] == 0.0
    assert result["trials_per_hour"] > 0.0
    assert result["speedup_vs_ex07"] > 0.0
    assert result["estimated_seconds_for_4096"] > 0.0
    assert result["timings"]["batch_count"] == 2
    assert result["environment"]["optuna"] == "4.9.0"
    assert not (experiment_dir / "runtime").exists()
    published = Path(result["benchmark_path"])
    assert published.is_file()
    assert json.loads(published.read_text(encoding="utf-8"))["completed_trials"] == 16
    assert not list(output_root.glob("*.tmp"))


@pytest.mark.parametrize("completed_trials", [0, 10])
def test_inmemory_benchmark_rejects_invalid_trial_counts(
    tmp_path: Path, completed_trials: int
) -> None:
    with pytest.raises(ValueError, match="positive multiple of 8"):
        run_inmemory_benchmark(
            tmp_path / "raw",
            tmp_path / "baselines",
            tmp_path / "experiment",
            tmp_path / "outputs",
            completed_trials=completed_trials,
        )
