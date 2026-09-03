"""Trader orchestration for immutable strategy-evaluation bundles."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from strategy_manager import Strategy, StrategyManagerError, StrategyRegistry, StrategyVersion, canonical_sha256
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
        str(value.get("family", "")), str(value.get("generation_stage", "")),
        None if value.get("parent_candidate_id") is None else str(value["parent_candidate_id"]),
        str(value.get("parameter_group", "")),
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
            comparisons = () if challenger is None or incumbent is None else compare_observation(challenger, incumbent, margins)
            if window != "full" and window not in protocol.target_windows:
                comparisons = tuple(item for item in comparisons if item.metric != "profit_factor")
            if not comparisons or not all(item.passed for item in comparisons):
                stress_ok = False
    champion_descriptor = next(item for item in candidates if item.candidate_id == champion)
    robust_ids = {item.candidate_id for item in ranking.profiles if item.eligible}
    neighbor_count = sum(
        item.candidate_id not in {champion, protocol.incumbent_id}
        and item.candidate_id in robust_ids
        and bool(champion_descriptor.parameter_group)
        and item.parameter_group == champion_descriptor.parameter_group
        and item.family == champion_descriptor.family
        for item in candidates
    )
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
        stored_hash = existing.pop("decision_hash", None)
        if stored_hash != _canonical_hash(existing):
            raise ValueError("completed evaluation decision hash mismatch")
        existing["decision_hash"] = stored_hash
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
    validate_protocol(protocol, candidates, formal, trials)
    ranking = rank_candidates(protocol, shortlist, formal, candidates)
    health = None
    stress: tuple[MetricObservation, ...] = ()
    repeated: tuple[MetricObservation, ...] = ()
    if ranking.champion_id:
        pair = (protocol.incumbent_id, ranking.champion_id)
        stress = runner(run_context, protocol, payloads, pair, "STRESS", ("fee_x2",))
        validate_protocol(protocol, candidates, stress, trials)
        repeated = runner(run_context, protocol, payloads, (ranking.champion_id,), "FORMAL")
        health = _health(protocol, ranking, formal, stress, repeated, candidates)
    result = finalize_evaluation(ranking, health, experiment_id)
    result_document = {**result.to_dict(), "input_hash": input_hash}
    result_document["decision_hash"] = _canonical_hash(result_document)

    comparisons = []
    by_key = {(x.candidate_id, x.window_id): x for x in formal}
    margins = resolve_margins(protocol)
    for candidate_id in shortlist.candidate_ids:
        for window in protocol.decision_windows:
            left, right = by_key.get((candidate_id, window)), by_key.get((protocol.incumbent_id, window))
            if left and right:
                values = compare_observation(left, right, margins)
                if window != "full" and window not in protocol.target_windows:
                    values = tuple(item for item in values if item.metric != "profit_factor")
                comparisons.extend(item.to_dict() for item in values)
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
        commit_order = [name for name in documents if name != "evaluation_result.json"]
        commit_order.append("evaluation_result.json")
        for name in commit_order:
            (temporary / name).replace(artifact_dir / name)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return CommandResult("PASS", "strategy.evaluate", result_document, {"directory": str(artifact_dir)})


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _winning_payload(experiment: Path, result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    protocol_raw = _read_object(experiment / "evaluation_protocol.json")
    protocol = EvaluationProtocol.from_dict(protocol_raw)
    manifest = _read_object(experiment / protocol.candidate_manifest)
    if result.get("input_hash") != _canonical_hash(protocol_raw, manifest):
        raise ValueError("evaluation input hash mismatch")
    integrity = dict(result)
    stored_hash = integrity.pop("decision_hash", None)
    if stored_hash != _canonical_hash(integrity):
        raise ValueError("evaluation decision hash mismatch")
    winner = result.get("recommended_candidate_id")
    candidates = manifest.get("candidates", [])
    match = next((item for item in candidates if isinstance(item, dict) and item.get("candidate_id") == winner), None)
    if match is None:
        raise ValueError("recommended candidate is missing from manifest")
    return manifest, match


def _find_source_version(registry: StrategyRegistry, strategy_id: str, experiment_id: str, candidate_id: str) -> StrategyVersion | None:
    try:
        registry.get_strategy(strategy_id)
    except StrategyManagerError:
        return None
    directory = registry.root / strategy_id / "versions"
    for path in sorted(directory.glob("v*.json")):
        version = registry.get_version(strategy_id, path.stem)
        if version.source_experiment == f"experiments/{experiment_id}" and str(version.source_candidate) == candidate_id:
            return version
    return None


def _full_metric(experiment: Path, candidate_id: str) -> dict[str, str]:
    with (experiment / "artifacts" / "formal_metrics.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    match = next((row for row in rows if row["candidate_id"] == candidate_id and row["window_id"] == "full"), None)
    if match is None:
        raise ValueError("winning candidate has no full-window formal metrics")
    return match


def _ensure_frozen(
    context: RepositoryContext,
    experiment_id: str,
    result: dict[str, Any],
    manifest: dict[str, Any],
    winner: dict[str, Any],
    actor: str,
    reason: str,
) -> StrategyVersion:
    registry = StrategyRegistry(context.strategy_root)
    strategy_id = str(winner.get("strategy_id", ""))
    if not strategy_id:
        raise ValueError("winning candidate requires strategy_id")
    try:
        registry.get_strategy(strategy_id)
    except StrategyManagerError:
        registry.create_strategy(
            Strategy.from_dict({
                "schema_version": 1, "strategy_id": strategy_id,
                "name": str(winner.get("strategy_name", strategy_id)),
                "objective": str(winner.get("strategy_objective", "执行已评估的交易策略")),
                "responsibility": str(winner.get("strategy_responsibility", "生成目标仓位；执行由交易模块负责")),
                "scope": [str(manifest["symbol"])], "created_at": datetime.now().astimezone().isoformat(), "created_by": actor,
            }), actor=actor, reason=reason,
        )
    candidate_id = str(winner["candidate_id"])
    version = _find_source_version(registry, strategy_id, experiment_id, candidate_id)
    expected_payload = winner.get("strategy_payload")
    if version is not None and version.strategy_payload != expected_payload:
        raise ValueError("existing source version payload does not match the winning candidate")
    if version is None:
        version_count = len(list((context.strategy_root / strategy_id / "versions").glob("v*.json")))
        version_name = f"v{version_count + 1}"
        parent = None if version_count == 0 else f"v{version_count}"
        cutoff = date.fromisoformat(str(_read_object(context.experiments_root / experiment_id / "evaluation_protocol.json")["development_cutoff"]))
        payload = winner.get("strategy_payload")
        if not isinstance(payload, dict):
            raise ValueError("winning candidate requires complete strategy_payload")
        version, _ = registry.create_version(
            StrategyVersion.from_dict({
                "schema_version": 1, "strategy_id": strategy_id, "version": version_name,
                "release_id": f"{strategy_id}-{version_name}", "parent_version": parent,
                "change_summary": f"Accept evaluation champion {candidate_id}",
                "source_experiment": f"experiments/{experiment_id}", "source_candidate": candidate_id,
                "selection_data_cutoff": cutoff.isoformat(),
                "forward_start": str(manifest.get("forward_start", (cutoff + timedelta(days=1)).isoformat())),
                "strategy_payload": payload, "release_hash": None,
            }), actor=actor, reason=reason,
        )
    qualification = registry.current_qualification(strategy_id, version.version)
    if qualification.value == "PAPER_READY":
        return registry.get_version(strategy_id, version.version)
    metric = _full_metric(context.experiments_root / experiment_id, candidate_id)
    calmar = metric.get("calmar")
    if calmar in {None, "", "None"}:
        raise ValueError("winning candidate requires a valid Calmar ratio")
    source = result.copy()
    evidence = {
        "schema_version": 1, "evidence_id": f"EVD-{strategy_id}-{version.version}-{experiment_id}",
        "strategy_id": strategy_id, "version": version.version, "release_hash": "0" * 64,
        "phase": "RESEARCH_BACKTEST", "period_start": str(manifest["windows"]["full"]["start"]),
        "period_end": str(manifest["windows"]["full"]["end"]), "data_identity": {"evaluation_input_hash": result["input_hash"]},
        "initial_capital": float(manifest.get("init_cash", 1_000_000)), "fee_rate": float(manifest.get("fee_rate", 0.0005)),
        "maximum_drawdown": float(metric["max_drawdown"]), "calmar_ratio": float(calmar),
        "win_loss_ratio": None, "win_loss_ratio_status": "UNAVAILABLE", "total_return": float(metric["total_return"]),
        "sharpe_ratio": None, "closed_trades": int(metric["closed_trades"]),
        "source_path": f"experiments/{experiment_id}/artifacts/evaluation_result.json", "source_hash": canonical_sha256(source),
        "recorded_at": datetime.now().astimezone().isoformat(), "recorded_by": actor,
    }
    frozen, _ = registry.freeze_version(strategy_id, version.version, actor=actor, reason=reason, evidence=evidence)
    return frozen


def accept_evaluation(
    context: RepositoryContext,
    experiment_id: str,
    actor: str,
    reason: str,
    *,
    pte_runner: Callable[..., Any] = subprocess.run,
) -> CommandResult:
    experiment = _experiment_path(context, experiment_id)
    result = _read_object(experiment / "artifacts" / "evaluation_result.json")
    if result.get("decision") != "RECOMMEND_FREEZE" or not result.get("recommended_candidate_id"):
        raise ValueError("evaluation decision must be RECOMMEND_FREEZE")
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
    manifest, winner = _winning_payload(experiment, result)
    journal_path = experiment / "evaluation_acceptance.json"
    journal = _read_object(journal_path) if journal_path.is_file() else None
    if journal is not None and journal.get("evaluation_input_hash") != result.get("input_hash"):
        raise ValueError("acceptance journal belongs to a different evaluation input")
    if journal is not None and journal.get("activation_state") == "PAPER_ACTIVE":
        return CommandResult("PASS", "strategy.accept-evaluation", journal)
    if journal is None:
        frozen = _ensure_frozen(context, experiment_id, result, manifest, winner, actor, reason)
        journal = {
            "schema_version": 1, "experiment_id": experiment_id,
            "evaluation_input_hash": result["input_hash"], "candidate_id": winner["candidate_id"],
            "strategy_id": frozen.strategy_id, "strategy_version": frozen.version,
            "release_id": frozen.release_id, "release_hash": frozen.release_hash,
            "sm_state": "PAPER_READY", "activation_state": "PAPER_ACTIVATION_PENDING",
            "actor": actor, "reason": reason, "accepted_at": datetime.now().astimezone().isoformat(),
        }
        _atomic_json(journal_path, journal)
    executable = context.root / ".venv" / "Scripts" / "pte.exe"
    command = [
        str(executable), "account", "create", "--repo-root", str(context.root),
        "--account-id", str(journal["release_id"]).lower(), "--name", str(winner.get("strategy_name", journal["release_id"])),
        "--strategy", str(journal["strategy_id"]), "--strategy-version", str(journal["strategy_version"]),
        "--symbol", str(manifest["symbol"]), "--initial-cash", "100000",
    ]
    try:
        completed = pte_runner(command, check=False, capture_output=True, text=True, encoding="utf-8")
        return_code = completed.returncode
        pte_error = (completed.stderr or completed.stdout or "PTE account registration failed").strip()
    except OSError as exc:
        return_code = 1
        pte_error = str(exc)
    warnings: tuple[str, ...] = ()
    if return_code == 0:
        journal["activation_state"] = "PAPER_ACTIVE"
        journal["activated_at"] = datetime.now().astimezone().isoformat()
        journal.pop("pte_error", None)
    else:
        journal["activation_state"] = "PAPER_ACTIVATION_PENDING"
        journal["pte_error"] = pte_error
        warnings = ("SM 已冻结；PTE 注册待重试",)
    _atomic_json(journal_path, journal)
    return CommandResult("PASS", "strategy.accept-evaluation", journal, warnings=warnings)
