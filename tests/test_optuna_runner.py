from __future__ import annotations

import json
from pathlib import Path

import optuna
import pandas as pd
import pytest
import czsc_trader.data as data_module
import czsc_trader.optuna_runner as runner_module

from czsc_trader.optuna_runner import (
    TrialEvaluationInputs,
    _study_from_protocol,
    build_trial_outcome,
    evaluate_trial_batch,
    evaluate_trial_request,
    prepare_selection_inputs,
    validate_candidate_identity,
    validate_ex07_protocol,
    validate_source_hash,
)
from czsc_trader.optuna_search import ProjectedStrategy, TrialRequest


PROTOCOL_PATH = Path("experiments/0824_EX07/artifacts/protocol.json")


def _metrics(strategy_return: float, sharpe: float) -> dict[str, object]:
    return {
        "strategy_return": strategy_return,
        "sharpe": sharpe,
        "max_drawdown": -0.1,
        "trade_count": 2,
        "exposure": 0.5,
    }


def test_formal_protocol_is_accepted_without_relaxing_holdout_boundary() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))

    validate_ex07_protocol(protocol)

    protocol["holdout_windows"] = ["2026H1"]
    with pytest.raises(ValueError, match="holdout windows"):
        validate_ex07_protocol(protocol)


def test_study_storage_modes_isolate_memory_from_sqlite(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    memory_runtime = tmp_path / "memory-runtime"
    sqlite_runtime = tmp_path / "sqlite-runtime"

    memory = _study_from_protocol(
        memory_runtime, protocol, "memory-digest", storage_mode="memory"
    )
    sqlite = _study_from_protocol(sqlite_runtime, protocol, "sqlite-digest")

    assert isinstance(memory._storage, optuna.storages.InMemoryStorage)
    assert not memory_runtime.exists()
    assert (sqlite_runtime / "optuna.db").is_file()
    with pytest.raises(ValueError, match="unsupported storage mode"):
        _study_from_protocol(tmp_path / "bad-runtime", protocol, "bad", storage_mode="bad")


def test_existing_cli_runs_ex08_with_formal_memory_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment_dir = tmp_path / "0824_EX08"
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "protocol.json").write_text(
        json.dumps({"experiment_id": "0824_EX08", "storage_mode": "memory"}),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run(raw_dir, baseline_root, selected_dir, **kwargs):
        captured.update(kwargs)
        return {"status": "FAIL", "experiment_dir": str(selected_dir)}

    monkeypatch.setattr(runner_module, "run_optuna_experiment", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "optuna_runner",
            "--experiment-dir",
            str(experiment_dir),
            "--storage-mode",
            "memory",
            "--require-full-trial-count",
        ],
    )

    runner_module.cli()

    assert captured["protocol_validator"] is runner_module.validate_ex08_protocol
    assert captured["storage_mode"] == "memory"
    assert captured["recover_runtime"] is False
    assert captured["collect_batch_timings"] is True
    assert captured["require_full_trial_count"] is True


def test_trial_objective_is_worst_return_delta_and_ignores_sharpe() -> None:
    weights = pd.Series({"factor": 1.0}, name="weight")
    strategy = ProjectedStrategy(weights, 0.2, 0.0, 1)
    baseline = {
        "A": _metrics(0.10, 100.0),
        "B": _metrics(0.20, 100.0),
    }
    challenger = {
        "A": _metrics(0.14, -100.0),
        "B": _metrics(0.19, -100.0),
    }

    outcome = build_trial_outcome(7, strategy, challenger, baseline, "abc")

    assert outcome.value == pytest.approx(-0.01)
    assert outcome.user_attrs["win_count"] == 1
    assert outcome.user_attrs["min_return_delta"] == pytest.approx(-0.01)
    assert outcome.user_attrs["window_metrics"]["A"]["sharpe"] == -100.0


def test_candidate_identity_requires_exact_names_and_order(tmp_path: Path) -> None:
    path = tmp_path / "factor_candidates.csv"
    pd.DataFrame({"factor": ["a", "b"], "type": ["base", "state"]}).to_csv(
        path, index=False, encoding="utf-8-sig"
    )

    proof = validate_candidate_identity(("a", "b"), path, expected_count=2)

    assert proof["status"] == "PASS"
    assert proof["candidate_factor_count"] == 2
    with pytest.raises(ValueError, match="identity"):
        validate_candidate_identity(("b", "a"), path, expected_count=2)


def test_source_hash_validation_detects_any_changed_byte(tmp_path: Path) -> None:
    path = tmp_path / "source.json"
    path.write_bytes(b"original")
    expected = "0682c5f2076f099c34cfdd15a9e063849ed437a49677e6fcc5b4198c76575be5"

    validate_source_hash(path, expected, "source")
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source hash"):
        validate_source_hash(path, expected, "source")


def test_selection_context_freezes_ex06_candidates_without_opening_2026(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    original = data_module._read_one

    def recording_read(path: Path, freq: str, symbol: str) -> pd.DataFrame:
        opened.append(path.name)
        return original(path, freq, symbol)

    monkeypatch.setattr(data_module, "_read_one", recording_read)

    context = prepare_selection_inputs(
        Path("data/raw"),
        Path("configs/rule_baselines"),
        Path("experiments/0824_EX07"),
    )

    assert context.data.daily["dt"].max() == pd.Timestamp("2025-12-31")
    assert context.candidate.factors.shape[1] == 91
    assert context.identity["status"] == "PASS"
    assert opened
    assert all("_2026.csv" not in name for name in opened)


def test_ex04_trial_replays_to_zero_return_delta() -> None:
    context = prepare_selection_inputs(
        Path("data/raw"),
        Path("configs/rule_baselines"),
        Path("experiments/0824_EX07"),
    )
    strategy = ProjectedStrategy(
        context.origin_weights,
        float(context.ex04["spec"]["enter"]),
        float(context.ex04["spec"]["exit"]),
        int(context.origin_weights.ne(0.0).sum()),
    )
    inputs = TrialEvaluationInputs.from_context(context)

    outcome = evaluate_trial_request(TrialRequest(0, strategy), inputs)

    assert outcome.value == pytest.approx(0.0, abs=1e-12)
    assert outcome.user_attrs["win_count"] == 0
    assert len(outcome.user_attrs["target_digest"]) == 64


def test_serial_and_parallel_trial_batches_are_identical() -> None:
    context = prepare_selection_inputs(
        Path("data/raw"),
        Path("configs/rule_baselines"),
        Path("experiments/0824_EX07"),
    )
    strategy = ProjectedStrategy(
        context.origin_weights,
        float(context.ex04["spec"]["enter"]),
        float(context.ex04["spec"]["exit"]),
        int(context.origin_weights.ne(0.0).sum()),
    )
    inputs = TrialEvaluationInputs.from_context(context)
    requests = (TrialRequest(0, strategy), TrialRequest(1, strategy))

    serial = evaluate_trial_batch(requests, inputs, n_jobs=1)
    parallel = evaluate_trial_batch(requests, inputs, n_jobs=2)

    assert serial == parallel
