"""Trader orchestration for immutable strategy-evaluation bundles."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
from strategy_evaluator import (
    CandidateDescriptor,
    EvaluationProtocol,
    HealthEvidence,
    HealthStatus,
    MetricObservation,
    TrialRecord,
    compare_observation,
    finalize_evaluation,
    rank_candidates,
    render_summary,
    resolve_margins,
    screen_candidates,
    validate_protocol,
)

from czsc_trader.candidate_evaluation import CandidateEvaluationContext, evaluate_candidate_payloads

from .context import RepositoryContext
from .results import CommandResult

Runner = Callable[..., tuple[MetricObservation, ...]]


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _canonical_hash(*values: object) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _experiment_path(context: RepositoryContext, experiment_id: str) -> Path:
    if not experiment_id or Path(experiment_id).name != experiment_id:
        raise ValueError("experiment must be one direct child name")
    path = (context.experiments_root / experiment_id).resolve()
    if path.parent != context.experiments_root.resolve() or not path.is_dir():
        raise ValueError(f"experiment does not exist: {experiment_id}")
    return path


def _descriptor(value: dict[str, Any]) -> CandidateDescriptor:
    return CandidateDescriptor(
        str(value["candidate_id"]), str(value.get("strategy_hash", value.get("candidate_hash", ""))),
        str(value["execution_policy_hash"]), bool(value.get("is_incumbent", False)),
        str(value.get("behavior_hash", "")), float(value.get("parameter_distance", 0.0)),
    )


def _trial(value: dict[str, Any]) -> TrialRecord:
    return TrialRecord.from_dict(value)


def _csv_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _health(
    protocol: EvaluationProtocol,
    ranking,
    formal: tuple[MetricObservation, ...],
    stress: tuple[MetricObservation, ...],
    repeated: tuple[MetricObservation, ...],
    candidates: tuple[CandidateDescriptor, ...],
) -> HealthEvidence | None:
    champion = ranking.champion_id
    if champion is None:
        return None
    reproducibility = HealthStatus.PASS if [x.to_dict() for x in repeated] == [x.to_dict() for x in formal if x.candidate_id == champion] else HealthStatus.FAIL
    margins = resolve_margins(protocol)
    stress_by_key = {(x.candidate_id, x.window_id, x.scenario_id): x for x in stress}
    stress_ok = True
    for scenario in {x.scenario_id for x in stress}:
        for window in protocol.decision_windows:
            challenger = stress_by_key.get((champion, window, scenario))
            incumbent = stress_by_key.get((protocol.incumbent_id, window, scenario))
            if challenger is None or incumbent is None or not all(item.passed for item in compare_observation(challenger, incumbent, margins)):
                stress_ok = False
    neighbor_count = sum(item.candidate_id != champion for item in candidates)
    return HealthEvidence(
        champion,
        HealthStatus.PASS,
        reproducibility,
        HealthStatus.PASS if neighbor_count else HealthStatus.INSUFFICIENT,
        HealthStatus.PASS if stress and stress_ok else HealthStatus.FAIL,
        HealthStatus.PASS,
    )


def evaluate_experiment(context: RepositoryContext, experiment_id: str, *, runner: Runner = evaluate_candidate_payloads) -> CommandResult:
    experiment = _experiment_path(context, experiment_id)
    protocol_raw = _read_object(experiment / "evaluation_protocol.json")
    protocol = EvaluationProtocol.from_dict(protocol_raw)
    if protocol.experiment_id != experiment_id:
        raise ValueError("protocol experiment_id does not match directory")
    manifest_path = (experiment / protocol.candidate_manifest).resolve()
    if manifest_path.parent != experiment or not manifest_path.is_file():
        raise ValueError("candidate manifest must be a direct experiment child")
    manifest = _read_object(manifest_path)
    raw_candidates = manifest.get("candidates")
    raw_trials = manifest.get("trials")
    if not isinstance(raw_candidates, list) or not isinstance(raw_trials, list):
        raise ValueError("candidate manifest requires candidates and trials lists")
    payloads = tuple(item for item in raw_candidates if isinstance(item, dict))
    candidates = tuple(_descriptor(item) for item in payloads)
    trials = tuple(_trial(item) for item in raw_trials if isinstance(item, dict))
    input_hash = _canonical_hash(protocol_raw, manifest)
    artifact_dir = experiment / "artifacts"
    result_path = artifact_dir / "evaluation_result.json"
    if result_path.is_file():
        existing = _read_object(result_path)
        if existing.get("input_hash") != input_hash:
            raise ValueError("completed evaluation has a different input hash")
        return CommandResult("PASS", "strategy.evaluate", existing, {"directory": str(artifact_dir)})

    windows_raw = manifest.get("windows")
    if not isinstance(windows_raw, dict) or not windows_raw:
        raise ValueError("candidate manifest requires windows")
    periods = tuple((str(name), (pd.Timestamp(value["start"]), pd.Timestamp(value["end"]))) for name, value in windows_raw.items() if isinstance(value, dict))
    run_context = CandidateEvaluationContext(
        context, str(manifest["symbol"]), str(manifest.get("asset_type", "etf")), periods,
        float(manifest.get("fee_rate", 0.0005)), float(manifest.get("init_cash", 1_000_000.0)),
    )
    preliminary = screen_candidates(protocol, candidates, ())
    screening_ids = (protocol.incumbent_id, *preliminary.candidate_ids)
    screening = runner(run_context, protocol, payloads, screening_ids, "SCREENING")
    validate_protocol(protocol, candidates, screening, trials)
    shortlist = screen_candidates(protocol, candidates, screening)
    formal_ids = (protocol.incumbent_id, *shortlist.candidate_ids)
    formal = runner(run_context, protocol, payloads, formal_ids, "FORMAL")
    ranking = rank_candidates(protocol, shortlist, formal, candidates)
    health = None
    stress: tuple[MetricObservation, ...] = ()
    repeated: tuple[MetricObservation, ...] = ()
    if ranking.champion_id:
        pair = (protocol.incumbent_id, ranking.champion_id)
        stress = runner(run_context, protocol, payloads, pair, "STRESS", ("fee_x2",))
        repeated = runner(run_context, protocol, payloads, (ranking.champion_id,), "FORMAL")
        health = _health(protocol, ranking, formal, stress, repeated, candidates)
    result = finalize_evaluation(ranking, health, experiment_id)
    result_document = {**result.to_dict(), "input_hash": input_hash}

    comparisons = []
    by_key = {(x.candidate_id, x.window_id): x for x in formal}
    margins = resolve_margins(protocol)
    for candidate_id in shortlist.candidate_ids:
        for window in protocol.decision_windows:
            left, right = by_key.get((candidate_id, window)), by_key.get((protocol.incumbent_id, window))
            if left and right:
                comparisons.extend(item.to_dict() for item in compare_observation(left, right, margins))
    documents = {
        "evaluation_result.json": json.dumps(result_document, ensure_ascii=False, indent=2) + "\n",
        "evaluation_report.md": render_summary(result),
        "formal_metrics.csv": _csv_text([item.to_dict() for item in formal]),
        "noninferiority.csv": _csv_text(comparisons),
        "pareto_profiles.csv": _csv_text([item.to_dict() for item in ranking.profiles]),
        "health_check.json": json.dumps(health.to_dict() if health else None, ensure_ascii=False, indent=2) + "\n",
        "trial_ledger.csv": _csv_text([item.to_dict() for item in trials]),
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".evaluation-", dir=experiment))
    try:
        for name, text in documents.items():
            (temporary / name).write_text(text, encoding="utf-8")
        for name in documents:
            (temporary / name).replace(artifact_dir / name)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return CommandResult("PASS", "strategy.evaluate", result_document, {"directory": str(artifact_dir)})
