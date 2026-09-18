"""TDR-owned freeze review and formal adjudication workflow."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from strategy_manager import (
    AdjudicationReport,
    CandidateSnapshot,
    EvaluationMandate,
    ReviewStatus,
    StrategyManagerError,
    StrategyRegistry,
    StrategyVersion,
    canonical_sha256,
)
from strategy_evaluator import EvaluationProtocol

from czsc_trader.experiment_archive import (
    resolve_experiment_dir,
    resolve_repository_experiment_reference,
)

from .context import RepositoryContext
from .errors import ValidationError
from .evaluation_service import evaluate_experiment
from .results import CommandResult
from .runtime_acceptance import validate_runtime_readiness


KNOWN_AUDITS = frozenset(
    {
        "objective_recalculation",
        "parameter_robustness",
        "statistical_robustness",
        "cost_stress",
        "technical_replay",
        "external_validation",
        "runtime_acceptance",
        "monitoring_plan",
    }
)

METRIC_ALIASES = {
    "annual_return": "net_cagr",
    "net_cagr": "net_cagr",
    "total_return": "total_return",
    "maximum_drawdown": "max_drawdown",
    "max_drawdown": "max_drawdown",
    "calmar_ratio": "calmar",
    "calmar": "calmar",
    "profit_factor": "profit_factor",
    "closed_trades": "closed_trades",
    "turnover": "turnover",
    "cost_drag": "cost_drag",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_object(context: RepositoryContext, path: Path) -> dict[str, Any]:
    resolved = path if path.is_absolute() else context.root / path
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {resolved}")
    return value


def _experiment_path(context: RepositoryContext, reference: str) -> Path:
    if reference.startswith("experiments/"):
        path = resolve_repository_experiment_reference(context.root, reference)
        if not path.is_dir():
            raise ValueError(f"source experiment does not exist: {reference}")
        return path
    return resolve_experiment_dir(context.experiments_root, reference)


def _hash_file(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def open_freeze_review(
    context: RepositoryContext,
    *,
    review_id: str,
    candidate_path: Path,
    mandate_path: Path,
    actor: str,
) -> CommandResult:
    """Human gate 2: freeze candidate, final mandate, and audit policy."""

    try:
        snapshot = CandidateSnapshot.from_dict(_read_object(context, candidate_path))
        mandate = EvaluationMandate.from_dict(_read_object(context, mandate_path))
        unknown = sorted(set(mandate.required_audits) - KNOWN_AUDITS)
        if unknown:
            raise ValueError(f"evaluation mandate has unsupported audits: {unknown}")
        if mandate.finalized_by != actor:
            raise ValueError("evaluation mandate finalizer differs from gate actor")
        experiment = _experiment_path(context, snapshot.source_experiment)
        if experiment.name not in snapshot.source_experiment:
            raise ValueError("candidate source experiment identity is ambiguous")
        case = StrategyRegistry(context.strategy_root).open_freeze_review(
            review_id, snapshot, mandate, actor=actor
        )
    except (StrategyManagerError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "freeze_review_open_failed",
            str(exc),
            context={"command": "strategy.review.open", "review_id": review_id},
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.review.open",
        {"review_case": case.to_dict()},
    )


def show_freeze_review(
    context: RepositoryContext, strategy_id: str, review_id: str
) -> CommandResult:
    try:
        registry = StrategyRegistry(context.strategy_root)
        case, snapshot, mandate = registry.get_freeze_review(strategy_id, review_id)
        report_path = context.strategy_root / strategy_id / "reviews" / review_id / "adjudication_report.json"
        report = None if not report_path.is_file() else json.loads(report_path.read_text(encoding="utf-8"))
    except (StrategyManagerError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "freeze_review_read_failed",
            str(exc),
            context={"command": "strategy.review.show", "review_id": review_id},
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.review.show",
        {
            "review_case": case.to_dict(),
            "candidate_snapshot": snapshot.to_dict(),
            "evaluation_mandate": mandate.to_dict(),
            "adjudication_report": report,
        },
    )


def _load_protocol_and_candidate(
    experiment: Path, snapshot: CandidateSnapshot
) -> tuple[EvaluationProtocol, dict[str, Any], dict[str, Any]]:
    protocol_raw = json.loads((experiment / "evaluation_protocol.json").read_text(encoding="utf-8"))
    protocol = EvaluationProtocol.from_dict(protocol_raw)
    manifest_path = (experiment / protocol.candidate_manifest).resolve()
    if manifest_path.parent != experiment.resolve() or not manifest_path.is_file():
        raise ValueError("candidate manifest must be a direct source-experiment child")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate manifest has no candidate list")
    candidate = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id")) == snapshot.candidate_id
        ),
        None,
    )
    if candidate is None:
        raise ValueError("review candidate is absent from source experiment")
    if candidate.get("strategy_payload") != snapshot.strategy_payload:
        raise ValueError("candidate snapshot payload differs from source experiment")
    manifest_policy_hash = candidate.get("execution_policy_hash")
    if manifest_policy_hash != canonical_sha256(snapshot.execution_policy):
        raise ValueError("candidate execution policy differs from source experiment")
    return protocol, manifest, candidate


def _assert_mandate_alignment(
    protocol: EvaluationProtocol,
    manifest: dict[str, Any],
    mandate: EvaluationMandate,
) -> None:
    if protocol.development_cutoff != mandate.development_cutoff:
        raise ValueError("evaluation development cutoff differs from mandate")
    if manifest.get("windows") != mandate.evaluation_windows:
        raise ValueError("evaluation windows differ from mandate")
    if str(mandate.benchmark.get("id")) != protocol.incumbent_id:
        raise ValueError("evaluation benchmark differs from mandate")
    primary_fee = mandate.cost_policy.get("primary_fee_rate")
    if primary_fee is None or float(manifest.get("fee_rate", -1)) != float(primary_fee):
        raise ValueError("evaluation primary cost differs from mandate")
    if str(manifest.get("forward_start")) != mandate.forward_start:
        raise ValueError("evaluation forward start differs from mandate")


def _formal_metric(experiment: Path, candidate_id: str) -> dict[str, str]:
    with (experiment / "artifacts" / "formal_metrics.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    match = next(
        (
            row
            for row in rows
            if row.get("candidate_id") == candidate_id
            and row.get("window_id") == "full"
            and row.get("scenario_id") == "standard"
        ),
        None,
    )
    if match is None:
        raise ValueError("candidate has no formal full-window metric")
    return match


def _number(value: Any) -> float:
    if value in {None, "", "None", "nan", "NaN"}:
        raise ValueError("metric is unavailable")
    return float(value)


def _claim_checks(snapshot: CandidateSnapshot, metric: dict[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for name, claim in snapshot.research_claims.items():
        metric_name = METRIC_ALIASES.get(name)
        if metric_name is None:
            checks.append(
                {"claim": name, "status": "UNVERIFIABLE", "reason": "unsupported metric"}
            )
            continue
        if isinstance(claim, dict):
            claimed = _number(claim.get("value"))
            tolerance = abs(_number(claim.get("tolerance", 0.0)))
        else:
            claimed = _number(claim)
            tolerance = 0.0
        recalculated = _number(metric.get(metric_name))
        difference = abs(recalculated - claimed)
        checks.append(
            {
                "claim": name,
                "claimed": claimed,
                "recalculated": recalculated,
                "tolerance": tolerance,
                "difference": difference,
                "status": "PASS" if difference <= tolerance else "CLAIM_MISMATCH",
            }
        )
    return checks


def _objective_checks(mandate: EvaluationMandate, metric: dict[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    operators = {
        ">=": lambda left, right: left >= right,
        ">": lambda left, right: left > right,
        "<=": lambda left, right: left <= right,
        "<": lambda left, right: left < right,
        "==": lambda left, right: left == right,
    }
    for objective in mandate.objectives:
        name = str(objective.get("metric"))
        metric_name = METRIC_ALIASES.get(name, name)
        operator = str(objective.get("operator"))
        if operator not in operators or metric_name not in metric:
            checks.append({"metric": name, "status": "INVALID_OBJECTIVE"})
            continue
        actual = _number(metric[metric_name])
        target = _number(objective.get("value"))
        checks.append(
            {
                "metric": name,
                "operator": operator,
                "target": target,
                "actual": actual,
                "status": "PASS" if operators[operator](actual, target) else "FAIL",
            }
        )
    return checks


def _prospective_runtime(
    registry: StrategyRegistry, snapshot: CandidateSnapshot, mandate: EvaluationMandate
) -> dict[str, Any]:
    versions = registry.versions(snapshot.strategy_id)
    version = f"v{len(versions) + 1}"
    placeholder_governance = {
        "review_id": "PROSPECTIVE",
        "candidate_snapshot_hash": "0" * 64,
        "evaluation_mandate_hash": "0" * 64,
        "adjudication_report_hash": "0" * 64,
        "human_decision_hash": "0" * 64,
        "runtime_acceptance_hash": "0" * 64,
    }
    draft = StrategyVersion.from_dict(
        {
            "schema_version": 2,
            "strategy_id": snapshot.strategy_id,
            "version": version,
            "release_id": f"{snapshot.strategy_id}-{version}",
            "parent_version": None if not versions else versions[-1].version,
            "change_summary": "prospective freeze review runtime",
            "source_experiment": snapshot.source_experiment,
            "source_candidate": snapshot.candidate_id,
            "selection_data_cutoff": mandate.development_cutoff,
            "forward_start": mandate.forward_start,
            "strategy_payload": snapshot.strategy_payload,
            "release_hash": None,
            "governance": placeholder_governance,
            "governance_hash": canonical_sha256(placeholder_governance),
        }
    )
    report = validate_runtime_readiness(draft)
    report["strategy_payload_hash"] = canonical_sha256(snapshot.strategy_payload)
    return report


def _artifact_audit(
    experiment: Path, audit: str, machine: dict[str, Any]
) -> dict[str, Any]:
    artifact = experiment / "artifacts"
    mapping = {
        "parameter_robustness": "parameter_neighborhood.csv",
        "statistical_robustness": "statistical_audit.json",
        "cost_stress": "execution_stress.csv",
        "external_validation": "external_validation.json",
        "monitoring_plan": "monitoring_plan.json",
    }
    path = artifact / mapping[audit]
    if not path.is_file() or path.stat().st_size == 0:
        return {"status": "MISSING", "artifact": mapping[audit]}
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
        if not reader.fieldnames or not rows:
            return {"status": "INCOMPLETE", "artifact": mapping[audit]}
    if path.suffix == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not document:
            return {"status": "INCOMPLETE", "artifact": mapping[audit]}
        if audit in {"external_validation", "monitoring_plan"} and document.get(
            "status"
        ) not in {"PASS", "APPROVED"}:
            return {"status": "INCOMPLETE", "artifact": mapping[audit], "detail": document}
    machine_checks = {
        "parameter_robustness": "parameter_robustness",
        "statistical_robustness": "statistical_robustness",
        "cost_stress": "cost_stress",
    }
    check_id = machine_checks.get(audit)
    if check_id is not None:
        checks = machine.get("checks")
        if not isinstance(checks, list):
            return {
                "status": "INCOMPLETE",
                "artifact": mapping[audit],
                "reason": "machine evaluation omitted checks",
            }
        check = next(
            (
                item
                for item in checks
                if isinstance(item, dict) and item.get("check_id") == check_id
            ),
            None,
        )
        if check is None:
            return {
                "status": "INCOMPLETE",
                "artifact": mapping[audit],
                "reason": f"machine evaluation omitted {check_id}",
            }
        status = str(check.get("status"))
        normalized = {
            "PASS": "PASS",
            "FAIL": "FAIL",
            "INSUFFICIENT": "INCOMPLETE",
            "NOT_APPLICABLE": "INCOMPLETE",
        }.get(status, "INCOMPLETE")
        return {
            "status": normalized,
            "artifact": mapping[audit],
            "machine_status": status,
            "reason_codes": list(check.get("reason_codes", [])),
            "metrics": dict(check.get("metrics", {})),
            "evidence_hash": _hash_file(path),
        }
    return {
        "status": "PASS",
        "artifact": mapping[audit],
        "evidence_hash": _hash_file(path),
    }


def evaluate_freeze_review(
    context: RepositoryContext, strategy_id: str, review_id: str
) -> CommandResult:
    """Independently recompute the candidate and issue one immutable report."""

    registry = StrategyRegistry(context.strategy_root)
    try:
        case, snapshot, mandate = registry.get_freeze_review(strategy_id, review_id)
        if case.status not in {ReviewStatus.OPEN, ReviewStatus.EVALUATING}:
            raise ValueError(f"freeze review cannot be evaluated: {case.status.value}")
        experiment = _experiment_path(context, snapshot.source_experiment)
        protocol, manifest, _candidate = _load_protocol_and_candidate(experiment, snapshot)
        _assert_mandate_alignment(protocol, manifest, mandate)
        evaluation = evaluate_experiment(
            context,
            experiment.name,
            use_cached_result=False,
            allow_artifact_reuse=False,
        )
        machine = evaluation.result.get("machine_evaluation")
        if not isinstance(machine, dict):
            raise ValueError("formal evaluation produced no machine report")
        if evaluation.result.get("recommended_candidate_id") != snapshot.candidate_id:
            raise ValueError("formal evaluation recommended another candidate")
        metric = _formal_metric(experiment, snapshot.candidate_id)
        claims = _claim_checks(snapshot, metric)
        objectives = _objective_checks(mandate, metric)
        runtime = _prospective_runtime(registry, snapshot, mandate)
        audit_results: dict[str, dict[str, Any]] = {}
        for audit in mandate.required_audits:
            if audit == "objective_recalculation":
                status = "PASS" if all(item["status"] == "PASS" for item in objectives) else "FAIL"
                audit_results[audit] = {
                    "status": status,
                    "checks": objectives,
                    "evidence_hash": _hash_file(experiment / "artifacts" / "formal_metrics.csv"),
                }
            elif audit == "runtime_acceptance":
                audit_results[audit] = {
                    "status": "PASS",
                    "runtime_sha256": runtime["runtime_sha256"],
                    "strategy_payload_hash": runtime["strategy_payload_hash"],
                    "evidence_hash": canonical_sha256(runtime),
                }
            elif audit == "technical_replay":
                metric_hash = evaluation.result.get("canonical_metric_hash")
                if not isinstance(metric_hash, str) or len(metric_hash) != 64:
                    audit_results[audit] = {
                        "status": "FAIL",
                        "reason": "independent replay produced no canonical metric hash",
                    }
                else:
                    audit_results[audit] = {
                        "status": "PASS",
                        "mode": "FULL_RECOMPUTE_WITHOUT_ARTIFACT_REUSE",
                        "canonical_metric_hash": metric_hash,
                        "evidence_hash": canonical_sha256(
                            {
                                "input_hash": evaluation.result.get("input_hash"),
                                "decision_hash": evaluation.result.get("decision_hash"),
                                "canonical_metric_hash": metric_hash,
                            }
                        ),
                    }
            else:
                audit_results[audit] = _artifact_audit(experiment, audit, machine)
        blocking: list[str] = []
        if any(item["status"] == "CLAIM_MISMATCH" for item in claims):
            blocking.append("CLAIM_MISMATCH")
        if any(item["status"] == "UNVERIFIABLE" for item in claims):
            blocking.append("UNVERIFIABLE_CLAIM")
        failed_audits = [
            name for name, result in audit_results.items() if result.get("status") == "FAIL"
        ]
        missing_audits = [
            name
            for name, result in audit_results.items()
            if result.get("status") in {"MISSING", "INCOMPLETE"}
        ]
        if failed_audits:
            blocking.extend(f"AUDIT_FAILED:{name}" for name in failed_audits)
        if missing_audits:
            blocking.extend(f"AUDIT_MISSING:{name}" for name in missing_audits)
        machine_verdict = str(machine.get("machine_verdict"))
        if machine_verdict != "ELIGIBLE_FOR_FREEZE_REVIEW":
            blocking.append(f"SE:{machine_verdict}")
        if missing_audits:
            verdict = "INCOMPLETE"
        elif blocking:
            verdict = "REJECTED"
        else:
            verdict = "ELIGIBLE_FOR_FREEZE_REVIEW"
        report_payload = {
            "schema_version": 1,
            "report_id": f"ADR-{review_id}",
            "review_id": review_id,
            "strategy_id": strategy_id,
            "candidate_id": snapshot.candidate_id,
            "candidate_hash": snapshot.candidate_hash,
            "evaluation_mandate_hash": mandate.mandate_hash,
            "audit_policy_hash": case.audit_policy_hash,
            "claim_checks": claims,
            "audit_results": audit_results,
            "machine_verdict": verdict,
            "risk_label": str(machine.get("risk_label") or "ADVERSE"),
            "blocking_findings": blocking,
            "reservations": list(evaluation.result.get("reason_codes", [])),
            "generated_at": _now(),
        }
        report = AdjudicationReport.from_dict(
            {**report_payload, "report_hash": canonical_sha256(report_payload)}
        )
        updated = registry.record_adjudication(report)
    except (StrategyManagerError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "freeze_review_evaluation_failed",
            str(exc),
            context={"command": "strategy.review.evaluate", "review_id": review_id},
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.review.evaluate",
        {"review_case": updated.to_dict(), "adjudication_report": report.to_dict()},
        {"source_experiment": str(experiment.relative_to(context.root))},
    )


def freeze_review_candidate(
    context: RepositoryContext,
    strategy_id: str,
    review_id: str,
    *,
    actor: str,
    reason: str,
    change_summary: str,
) -> CommandResult:
    """Human gate 3: revalidate runtime and atomically create the frozen version."""

    registry = StrategyRegistry(context.strategy_root)
    try:
        case, snapshot, mandate = registry.get_freeze_review(strategy_id, review_id)
        if case.status is ReviewStatus.FROZEN:
            version, event = registry.create_frozen_version(
                strategy_id,
                review_id,
                actor=actor,
                reason=reason,
                human_decision={},
                runtime_acceptance={},
                evidence={},
                change_summary=change_summary,
            )
            return CommandResult(
                "PASS",
                "strategy.freeze",
                {
                    "version": version.to_dict(),
                    "event": event.to_dict(),
                    "review_id": review_id,
                    "pte_deployment": "NOT_REQUESTED",
                    "idempotent_replay": True,
                },
            )
        if case.status is not ReviewStatus.ELIGIBLE:
            raise ValueError(f"freeze review is not eligible: {case.status.value}")
        decision_payload = {
            "schema_version": 1,
            "review_id": review_id,
            "strategy_id": strategy_id,
            "candidate_id": snapshot.candidate_id,
            "candidate_hash": snapshot.candidate_hash,
            "decision": "APPROVE_FREEZE",
            "actor": actor,
            "reason": reason,
            "decided_at": _now(),
        }
        human_decision = {
            **decision_payload,
            "decision_hash": canonical_sha256(decision_payload),
        }
        runtime = _prospective_runtime(registry, snapshot, mandate)
        experiment = _experiment_path(context, snapshot.source_experiment)
        _protocol, manifest, _candidate = _load_protocol_and_candidate(
            experiment, snapshot
        )
        metric = _formal_metric(experiment, snapshot.candidate_id)
        evidence = {
            "schema_version": 1,
            "evidence_id": f"EVD-{strategy_id}-{snapshot.candidate_id}-{review_id}",
            "strategy_id": strategy_id,
            "version": "v1",
            "release_hash": "0" * 64,
            "phase": "RESEARCH_BACKTEST",
            "period_start": str(mandate.evaluation_windows["full"]["start"]),
            "period_end": str(mandate.evaluation_windows["full"]["end"]),
            "data_identity": {
                "candidate_snapshot_hash": case.candidate_snapshot_hash,
                "evaluation_mandate_hash": mandate.mandate_hash,
            },
            "initial_capital": float(manifest.get("init_cash", 1_000_000.0)),
            "fee_rate": float(mandate.cost_policy["primary_fee_rate"]),
            "maximum_drawdown": _number(metric["max_drawdown"]),
            "calmar_ratio": _number(metric["calmar"]),
            "win_loss_ratio": None
            if metric.get("profit_factor") in {None, "", "None"}
            else _number(metric["profit_factor"]),
            "win_loss_ratio_status": str(metric.get("profit_factor_status", "UNAVAILABLE")),
            "total_return": _number(metric["total_return"]),
            "sharpe_ratio": None,
            "closed_trades": int(metric["closed_trades"]),
            "source_path": str(
                (experiment / "artifacts" / "evaluation_result.json").relative_to(context.root)
            ).replace("\\", "/"),
            "source_hash": _hash_file(experiment / "artifacts" / "evaluation_result.json"),
            "recorded_at": _now(),
            "recorded_by": actor,
        }
        version, event = registry.create_frozen_version(
            strategy_id,
            review_id,
            actor=actor,
            reason=reason,
            human_decision=human_decision,
            runtime_acceptance=runtime,
            evidence=evidence,
            change_summary=change_summary,
        )
    except (StrategyManagerError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "strategy_freeze_failed",
            str(exc),
            context={"command": "strategy.freeze", "review_id": review_id},
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.freeze",
        {
            "version": version.to_dict(),
            "event": event.to_dict(),
            "review_id": review_id,
            "pte_deployment": "NOT_REQUESTED",
        },
    )
