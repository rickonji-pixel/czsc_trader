"""Formal EX08 pure-memory full Optuna search."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .optuna_runner import run_optuna_experiment


VALIDATION_WINDOWS = (
    "2022H1",
    "2022H2",
    "2023H1",
    "2023H2",
    "2024H1",
    "2024H2",
    "2025H1",
    "2025H2",
)
HOLDOUT_WINDOWS = ("2026Q1", "2026H1", "2026M1-M8")


def validate_ex08_protocol(protocol: Mapping[str, object]) -> None:
    """Require the exact preregistered single-variable EX08 contract."""
    expected_top = {
        "schema_version": 1,
        "experiment_id": "0824_EX08",
        "experiment_type": "optuna_inmemory_full_joint_strategy_search",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "selection_sample_end": "2025-12-31",
        "selection_metric": "minimum_return_delta_vs_ex04",
        "pass_rule": "strictly higher return than EX04 in every 2026 holdout window",
        "holdout_access_before_freeze": False,
        "storage_mode": "memory",
        "runtime_database": None,
        "runtime_database_tracked": False,
        "staged_search": False,
    }
    expected_baseline = {
        "experiment": "0824_EX04",
        "file": "experiments/0824_EX04/artifacts/frozen_challenger.json",
        "sha256": "c6fa86c0f87743564231dec4fb6e66971bde0fa4107829d077cb5cf31c53885f",
    }
    expected_source = {
        "experiment": "0824_EX06",
        "protocol_sha256": "e47b31a21d5b309553ec2819878cabaabb0b7f55e54a5a03e0def9c25666649f",
        "factor_candidates_sha256": "a27eec780e9b49951200467468853ebdc0ac160de1fda6b6c9a50e4e0625e4eb",
        "factor_universe_sha256": "7ac73a46db6210f9b2b79d16780a9985cdd3a509b229217d71e10f7cdb408f9f",
        "frozen_challenger_sha256": "868e989439e4442b4637f7976f438705e61b1d023f688ab7ce9d2ee1d0ac7b89",
        "candidate_names_sha256": "19d0c0d9eabf9147f1f84c68ba8a24eb061b4b93db12d22a0e1b1e86567252b9",
        "candidate_factor_count": 91,
    }
    expected_optuna = {
        "version": "4.9.0",
        "sampler": "TPESampler",
        "seed": 20260824,
        "n_startup_trials": 256,
        "multivariate": True,
        "group": False,
        "constant_liar": True,
        "maximum_completed_trials": 4096,
        "minimum_completed_trials": 4096,
        "no_improvement_trials": None,
        "maximum_wall_time_seconds": None,
        "batch_size": 8,
    }
    expected_parallel = {
        "backend": "loky",
        "n_jobs": 8,
        "inner_max_num_threads": 1,
        "database_writer": "none",
        "result_order": "trial_number",
    }
    expected_projection = {
        "active_factor_count": {"low": 6, "high": 18},
        "raw_weight_bounds": [-1.0, 1.0],
        "minimum_absolute_weight": 0.0125,
        "minimum_trend_weight": 0.1,
        "protect_volume_window": True,
        "enter_threshold_bounds": [0.05, 0.3],
        "exit_gap_bounds": [0.025, 0.25],
    }
    actual_top = {key: protocol.get(key) for key in expected_top}
    actual_projection = {key: protocol.get(key) for key in expected_projection}
    if (
        actual_top != expected_top
        or protocol.get("research_baseline") != expected_baseline
        or protocol.get("candidate_source") != expected_source
        or tuple(protocol.get("validation_windows", ())) != VALIDATION_WINDOWS
        or tuple(protocol.get("holdout_windows", ())) != HOLDOUT_WINDOWS
        or protocol.get("optuna") != expected_optuna
        or protocol.get("parallel") != expected_parallel
        or actual_projection != expected_projection
    ):
        raise ValueError("EX08 protocol differs from preregistration")


def run_ex08_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    """Run the preregistered nonrecoverable EX08 study and one holdout."""
    return run_optuna_experiment(
        raw_dir,
        baseline_root,
        experiment_dir,
        protocol_validator=validate_ex08_protocol,
        storage_mode="memory",
        recover_runtime=False,
        collect_batch_timings=True,
        require_full_trial_count=True,
        execution_commit=execution_commit,
    )
