from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

import czsc_trader.data as data_module
from czsc_trader.optuna_runner import (
    _write_research_docs,
    prepare_selection_inputs,
    validate_ex08_protocol,
)


PROTOCOL_PATH = Path("experiments/0824_EX08/artifacts/protocol.json")


def _protocol() -> dict[str, object]:
    return json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))


def test_ex08_protocol_accepts_only_the_preregistered_exact_count_memory_search() -> None:
    validate_ex08_protocol(_protocol())

    mutations = [
        ("storage_mode", "sqlite"),
        ("experiment_id", "0824_EX07"),
    ]
    for key, value in mutations:
        changed = _protocol()
        changed[key] = value
        with pytest.raises(ValueError, match="EX08 protocol"):
            validate_ex08_protocol(changed)

    optuna_mutations = [
        ("maximum_completed_trials", 1024),
        ("minimum_completed_trials", 1024),
        ("maximum_wall_time_seconds", 7200),
        ("no_improvement_trials", 1024),
        ("seed", 1),
    ]
    for key, value in optuna_mutations:
        changed = _protocol()
        changed["optuna"][key] = value
        with pytest.raises(ValueError, match="EX08 protocol"):
            validate_ex08_protocol(changed)


def test_ex08_selection_context_never_opens_2026(
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
        Path("experiments/0824_EX08"),
        protocol_validator=validate_ex08_protocol,
    )

    assert context.data.daily["dt"].max() == pd.Timestamp("2025-12-31")
    assert context.identity["names_sha256"] == (
        "19d0c0d9eabf9147f1f84c68ba8a24eb061b4b93db12d22a0e1b1e86567252b9"
    )
    assert opened
    assert all("_2026.csv" not in name for name in opened)


def test_ex08_protocol_rejects_changed_candidate_or_parallel_contract() -> None:
    changed_candidate = deepcopy(_protocol())
    changed_candidate["candidate_source"]["candidate_factor_count"] = 90
    with pytest.raises(ValueError, match="EX08 protocol"):
        validate_ex08_protocol(changed_candidate)

    changed_parallel = deepcopy(_protocol())
    changed_parallel["parallel"]["database_writer"] = "parent_only"
    with pytest.raises(ValueError, match="EX08 protocol"):
        validate_ex08_protocol(changed_parallel)


def test_research_documents_use_the_actual_experiment_id(tmp_path: Path) -> None:
    summary = {
        "completed_trials": 4096,
        "failed_trials": 0,
        "stop_reason": "maximum_completed_trials",
        "elapsed_seconds": 1.0,
    }
    frozen = {
        "experiment": "0824_EX08",
        "trial_number": 12,
        "selection_objective": 0.01,
        "factor_names": ["a"],
        "enter": 0.2,
        "exit": 0.0,
    }
    window = {
        "ex04_return": 0.1,
        "challenger_return": 0.11,
        "return_delta_vs_ex04": 0.01,
        "ex04_sharpe": 1.0,
        "challenger_sharpe": 1.1,
        "pass": True,
    }
    holdout = {
        "status": "PASS",
        "audit": {"status": "PASS"},
        "windows": {name: window for name in ("2026Q1", "2026H1", "2026M1-M8")},
    }

    _write_research_docs(tmp_path, summary, frozen, holdout)

    conclusion = (tmp_path / "04_conclusion.md").read_text(encoding="utf-8")
    assert "EX08 Optuna联合搜索结果" in conclusion
    assert "EX08收益" in conclusion
    assert "EX07" not in conclusion
