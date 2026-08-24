"""Engineering-only in-memory throughput benchmark for the EX07 search path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any

import numpy as np
import optuna
import pandas as pd

from .optuna_runner import (
    TrialEvaluationInputs,
    _study_from_protocol,
    evaluate_trial_batch,
    prepare_selection_inputs,
)
from .optuna_search import (
    TrialRequest,
    enqueue_initial_trial,
    run_study_batches,
    suggest_trial_parameters,
    trial_params_for_strategy,
)


EX07_TRIALS_PER_HOUR = 301.95
PHASE_COLUMNS = {
    "ask_and_project": "ask_and_project_seconds",
    "parallel_evaluate": "parallel_evaluate_seconds",
    "tell_and_attrs": "tell_and_attrs_seconds",
}


def summarize_batch_timings(
    rows: Sequence[Mapping[str, float | int]],
) -> dict[str, object]:
    """Summarize hand-timed batch phases without changing search behavior."""
    if not rows:
        raise ValueError("batch timings must not be empty")
    total_seconds = float(sum(float(row["batch_total_seconds"]) for row in rows))
    phases: dict[str, dict[str, float]] = {}
    for name, column in PHASE_COLUMNS.items():
        values = np.asarray([float(row[column]) for row in rows], dtype=float)
        phase_total = float(values.sum())
        phases[name] = {
            "total_seconds": phase_total,
            "mean_seconds": float(values.mean()),
            "median_seconds": float(np.median(values)),
            "p95_seconds": float(np.percentile(values, 95)),
            "max_seconds": float(values.max()),
            "share": phase_total / total_seconds if total_seconds else 0.0,
        }
    return {
        "batch_count": len(rows),
        "total_seconds": total_seconds,
        "mean_batch_seconds": total_seconds / len(rows),
        "median_batch_seconds": float(
            np.median([float(row["batch_total_seconds"]) for row in rows])
        ),
        "p95_batch_seconds": float(
            np.percentile([float(row["batch_total_seconds"]) for row in rows], 95)
        ),
        "max_batch_seconds": max(float(row["batch_total_seconds"]) for row in rows),
        "phases": phases,
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_inmemory_benchmark(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    output_root: Path,
    *,
    completed_trials: int,
) -> dict[str, object]:
    """Run a fixed-size, pre-2026 EX07 benchmark with no persistent Study."""
    if completed_trials <= 0 or completed_trials % 8:
        raise ValueError("completed_trials must be a positive multiple of 8")

    raw_dir = Path(raw_dir)
    baseline_root = Path(baseline_root)
    experiment_dir = Path(experiment_dir)
    output_root = Path(output_root)
    context = prepare_selection_inputs(raw_dir, baseline_root, experiment_dir)
    latest_visible = pd.Timestamp(context.data.daily["dt"].max())
    selection_end = pd.Timestamp(context.protocol["selection_sample_end"])
    if latest_visible > selection_end or selection_end != pd.Timestamp("2025-12-31"):
        raise ValueError("benchmark selection data must end no later than 2025-12-31")

    protocol_path = experiment_dir / "artifacts" / "protocol.json"
    protocol_digest = sha256(protocol_path.read_bytes()).hexdigest()
    study = _study_from_protocol(
        experiment_dir / "runtime",
        context.protocol,
        protocol_digest,
        storage_mode="memory",
    )
    initial_params = trial_params_for_strategy(
        context.origin_weights,
        float(context.ex04["spec"]["enter"]),
        float(context.ex04["spec"]["exit"]),
        context.protocol,
    )
    enqueue_initial_trial(study, initial_params)
    inputs = TrialEvaluationInputs.from_context(context)
    factor_names = tuple(context.candidate.factors.columns)

    def request_factory(trial: optuna.Trial) -> TrialRequest:
        strategy = suggest_trial_parameters(
            trial, factor_names, context.origin_weights, context.protocol
        )
        return TrialRequest(trial.number, strategy)

    parallel = context.protocol["parallel"]
    config = context.protocol["optuna"]
    search = run_study_batches(
        study,
        request_factory,
        lambda requests: evaluate_trial_batch(
            requests, inputs, n_jobs=int(parallel["n_jobs"])
        ),
        maximum_completed_trials=completed_trials,
        minimum_completed_trials=completed_trials,
        no_improvement_trials=completed_trials + 1,
        maximum_wall_time_seconds=86_400.0,
        batch_size=int(config["batch_size"]),
        collect_batch_timings=True,
    )
    if int(search["completed_trials"]) != completed_trials:
        raise AssertionError("in-memory benchmark did not complete the requested trials")
    failed_trials = sum(
        trial.state == optuna.trial.TrialState.FAIL for trial in study.trials
    )
    if failed_trials:
        raise AssertionError(f"in-memory benchmark has {failed_trials} failed trials")
    trial_zero = study.trials[0]
    if trial_zero.value != 0.0:
        raise AssertionError("Trial 0 does not exactly replay EX04")

    elapsed_seconds = float(search["elapsed_seconds"])
    trials_per_hour = completed_trials / elapsed_seconds * 3600.0
    batch_rows = list(search["batch_timings"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = (
        output_root / f"ex07_inmemory_{completed_trials:04d}_{timestamp}.json"
    ).resolve()
    result: dict[str, object] = {
        "benchmark_kind": "engineering_only_ex07_inmemory",
        "research_evidence": False,
        "storage_mode": "memory",
        "completed_trials": completed_trials,
        "failed_trials": failed_trials,
        "selection_sample_end": str(selection_end.date()),
        "latest_visible_date": str(latest_visible.date()),
        "holdout_accessed": False,
        "trial_zero_objective": float(trial_zero.value),
        "elapsed_seconds": elapsed_seconds,
        "trials_per_hour": trials_per_hour,
        "baseline_ex07_trials_per_hour": EX07_TRIALS_PER_HOUR,
        "speedup_vs_ex07": trials_per_hour / EX07_TRIALS_PER_HOUR,
        "estimated_seconds_for_4096": 4096 / trials_per_hour * 3600.0,
        "timings": summarize_batch_timings(batch_rows),
        "batch_timings": batch_rows,
        "environment": {
            "python": platform.python_version(),
            "optuna": version("optuna"),
            "joblib": version("joblib"),
            "logical_cpu_count": os.cpu_count(),
            "n_jobs": int(parallel["n_jobs"]),
            "batch_size": int(config["batch_size"]),
            "command": " ".join([sys.executable, *sys.argv]),
        },
        "benchmark_path": str(output_path),
    }
    _write_json_atomic(output_path, result)
    return result
