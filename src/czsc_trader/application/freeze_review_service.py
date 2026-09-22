"""TDR-owned freeze review and formal adjudication workflow."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from strategy_manager import (
    AdjudicationReport,
    CandidateSnapshot,
    EvaluationMandate,
    GovernanceResult,
    GovernanceStage,
    StrategyManagerError,
    StrategyGovernanceCredential,
    StrategyGovernanceSeal,
    StrategyRegistry,
    StrategyVersion,
    canonical_sha256,
)
from strategy_evaluator import (
    EvaluationProtocol,
    ExternalReplayEvidence,
    MachineEvaluationPolicy,
    RiskLabel,
    required_stress_scenarios,
)
from trading_execution_engine import EXECUTION_CONTRACT_VERSION
from strategy_runtime import StrategyRuntimeError

from czsc_trader.experiment_archive import (
    resolve_experiment_dir,
    resolve_repository_experiment_reference,
)
from czsc_trader.candidate_evaluation import METRIC_SEMANTICS_VERSION

from .context import RepositoryContext
from .errors import ValidationError
from .evaluation_service import evaluate_experiment
from .results import CommandResult
from .review_data import publish_review_dataset, verify_review_dataset
from .runtime_acceptance import (
    require_same_runtime_content,
    validate_candidate_readiness,
    validate_runtime_readiness,
)


COMPLETE_AUDITS = frozenset(
    {
        "objective_recalculation",
        "frequency_recalculation",
        "parameter_robustness",
        "statistical_robustness",
        "cost_stress",
        "technical_replay",
        "external_validation",
        "runtime_acceptance",
        "monitoring_plan",
    }
)
KNOWN_AUDITS = COMPLETE_AUDITS

AUDIT_REQUIREMENT_KEYS = frozenset(
    {
        "parameter_robustness",
        "statistical_robustness",
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


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _validate_candidate_contract(snapshot: CandidateSnapshot) -> None:
    data_contract = snapshot.data_contract
    if set(data_contract) != {"symbol", "asset_type", "requirements"}:
        raise ValueError(
            "candidate data_contract must contain symbol, asset_type, and requirements"
        )
    if not isinstance(data_contract["symbol"], str) or not data_contract["symbol"].strip():
        raise ValueError("candidate data_contract symbol must be nonblank")
    if not isinstance(data_contract["asset_type"], str) or not data_contract["asset_type"].strip():
        raise ValueError("candidate data_contract asset_type must be nonblank")
    requirements = data_contract["requirements"]
    if (
        not isinstance(requirements, list)
        or not requirements
        or any(not isinstance(item, dict) or not item for item in requirements)
    ):
        raise ValueError("candidate data_contract requirements must be a nonempty list")
    names = [str(item.get("name", "")) for item in requirements]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("candidate data_contract requirement names must be unique and nonblank")
    execution = snapshot.execution_policy
    if set(execution) != {"policy_type", "settings"}:
        raise ValueError("candidate execution_policy must contain policy_type and settings")
    if not isinstance(execution["policy_type"], str) or not execution["policy_type"].strip():
        raise ValueError("candidate execution_policy policy_type must be nonblank")
    if not isinstance(execution["settings"], dict) or not execution["settings"]:
        raise ValueError("candidate execution_policy settings must be a nonempty object")


def _validate_mandate_contract(mandate: EvaluationMandate) -> None:
    required = set(mandate.required_audits)
    if required != COMPLETE_AUDITS:
        missing = sorted(COMPLETE_AUDITS - required)
        extra = sorted(required - COMPLETE_AUDITS)
        raise ValueError(
            f"evaluation mandate must require the complete audit matrix; "
            f"missing={missing}, extra={extra}"
        )
    if set(mandate.audit_requirements) != AUDIT_REQUIREMENT_KEYS:
        missing = sorted(AUDIT_REQUIREMENT_KEYS - set(mandate.audit_requirements))
        extra = sorted(set(mandate.audit_requirements) - AUDIT_REQUIREMENT_KEYS)
        raise ValueError(
            f"evaluation audit requirements are incomplete; missing={missing}, extra={extra}"
        )
    if any(
        not isinstance(mandate.audit_requirements[name], dict)
        or not mandate.audit_requirements[name]
        for name in AUDIT_REQUIREMENT_KEYS
    ):
        raise ValueError("each audit requirement must be a nonempty object")
    expected_requirement_fields = {
        "parameter_robustness": {"minimum_valid_neighbors"},
        "statistical_robustness": {
            "allowed_risk_labels",
            "minimum_bootstrap_probability",
            "maximum_pbo",
            "minimum_dsr_probability",
        },
        "technical_replay": {"mode", "allow_artifact_reuse", "execution_engine"},
        "external_validation": {
            "required_replays",
            "minimum_cagr",
            "max_drawdown_floor",
        },
        "runtime_acceptance": {"required_status"},
        "monitoring_plan": {"required_status", "minimum_rules"},
    }
    for name, fields in expected_requirement_fields.items():
        if set(mandate.audit_requirements[name]) != fields:
            raise ValueError(f"{name} audit requirement fields are invalid")
    parameter = mandate.audit_requirements["parameter_robustness"]
    if (
        type(parameter["minimum_valid_neighbors"]) is not int
        or parameter["minimum_valid_neighbors"] < 1
    ):
        raise ValueError("parameter minimum_valid_neighbors must be a positive integer")
    statistical = mandate.audit_requirements["statistical_robustness"]
    for field in (
        "minimum_bootstrap_probability",
        "maximum_pbo",
        "minimum_dsr_probability",
    ):
        value = _finite_number(statistical[field], f"statistical {field}")
        if not 0 <= value <= 1:
            raise ValueError(f"statistical {field} must be in [0, 1]")
    technical = mandate.audit_requirements["technical_replay"]
    if technical != {
        "mode": "FULL_RECOMPUTE",
        "allow_artifact_reuse": False,
        "execution_engine": EXECUTION_CONTRACT_VERSION,
    }:
        raise ValueError("technical replay must require a full recompute without artifact reuse")
    runtime = mandate.audit_requirements["runtime_acceptance"]
    if runtime["required_status"] != "PASS":
        raise ValueError("runtime acceptance must require PASS")
    monitoring = mandate.audit_requirements["monitoring_plan"]
    if monitoring["required_status"] != "APPROVED":
        raise ValueError("monitoring plan must require APPROVED")
    if type(monitoring["minimum_rules"]) is not int or monitoring["minimum_rules"] < 1:
        raise ValueError("monitoring minimum_rules must be a positive integer")

    windows = mandate.evaluation_windows
    full = windows.get("full")
    if not isinstance(full, dict):
        raise ValueError("evaluation windows must contain full")
    for name, value in windows.items():
        if not isinstance(value, dict) or set(value) != {"start", "end"}:
            raise ValueError(f"evaluation window {name} must contain start and end")
        try:
            start, end = date.fromisoformat(str(value["start"])), date.fromisoformat(
                str(value["end"])
            )
        except ValueError as exc:
            raise ValueError(f"evaluation window {name} has invalid dates") from exc
        if start > end:
            raise ValueError(f"evaluation window {name} starts after it ends")
        if end > date.fromisoformat(mandate.development_cutoff):
            raise ValueError(f"evaluation window {name} exceeds development cutoff")
    if str(full["end"]) != mandate.development_cutoff:
        raise ValueError("full evaluation window must end at development cutoff")

    benchmark = mandate.benchmark
    if set(benchmark) != {"type", "id"} or not all(
        isinstance(benchmark[name], str) and benchmark[name].strip() for name in ("type", "id")
    ):
        raise ValueError("evaluation benchmark must contain nonblank type and id")

    objective_names: list[str] = []
    for objective in mandate.objectives:
        if set(objective) != {"metric", "operator", "value"}:
            raise ValueError("each objective must contain metric, operator, and value")
        metric = str(objective["metric"]).strip()
        if not metric:
            raise ValueError("objective metric must be nonblank")
        if objective["operator"] not in {">=", ">", "<=", "<", "=="}:
            raise ValueError(f"objective {metric} has unsupported operator")
        _finite_number(objective["value"], f"objective {metric} value")
        objective_names.append(metric)
    if len(objective_names) != len(set(objective_names)):
        raise ValueError("evaluation objective metrics must be unique")

    cost = mandate.cost_policy
    required_cost = {
        "primary_fee_rate",
        "stress_scenarios",
        "blocking_scenarios",
        "minimum_cagr",
        "max_drawdown_floor",
        "minimum_calmar",
    }
    if set(cost) != required_cost:
        raise ValueError("cost_policy fields are incomplete")
    primary_fee = _finite_number(cost["primary_fee_rate"], "primary_fee_rate")
    if not 0 <= primary_fee < 1:
        raise ValueError("primary_fee_rate must be in [0, 1)")
    scenarios = cost["stress_scenarios"]
    blocking = cost["blocking_scenarios"]
    if (
        not isinstance(scenarios, list)
        or not scenarios
        or any(not isinstance(item, str) or not item for item in scenarios)
        or len(scenarios) != len(set(scenarios))
    ):
        raise ValueError("stress_scenarios must be a nonempty unique list")
    if (
        not isinstance(blocking, list)
        or not blocking
        or any(item not in scenarios for item in blocking)
    ):
        raise ValueError("blocking_scenarios must be a nonempty subset of stress_scenarios")
    standard_scenarios = {item.scenario_id for item in required_stress_scenarios()}
    if not standard_scenarios.issubset(scenarios):
        raise ValueError("stress_scenarios omit the standard diagnostic tiers")
    _finite_number(cost["minimum_cagr"], "cost minimum_cagr")
    drawdown_floor = _finite_number(cost["max_drawdown_floor"], "cost max_drawdown_floor")
    if not -1 <= drawdown_floor <= 0:
        raise ValueError("cost max_drawdown_floor must be in [-1, 0]")
    _finite_number(cost["minimum_calmar"], "cost minimum_calmar")

    frequency = mandate.frequency_policy
    if set(frequency) not in (
        {"mode", "window_days"},
        {"mode", "window_days", "minimum_closed_trades"},
    ):
        raise ValueError("frequency_policy fields are invalid")
    if frequency.get("mode") not in {"OBSERVE", "HARD"}:
        raise ValueError("frequency_policy mode must be OBSERVE or HARD")
    if type(frequency.get("window_days")) is not int or frequency["window_days"] <= 0:
        raise ValueError("frequency_policy window_days must be a positive integer")
    if frequency["mode"] == "HARD":
        if type(frequency.get("minimum_closed_trades")) is not int:
            raise ValueError("HARD frequency policy requires integer minimum_closed_trades")
        if frequency["minimum_closed_trades"] < 0:
            raise ValueError("minimum_closed_trades must be nonnegative")


def _machine_policy_from_mandate(mandate: EvaluationMandate) -> MachineEvaluationPolicy:
    statistical = mandate.audit_requirements["statistical_robustness"]
    external = mandate.audit_requirements["external_validation"]
    allowed_raw = statistical.get("allowed_risk_labels")
    if (
        not isinstance(allowed_raw, list)
        or not allowed_raw
        or any(item not in {label.value for label in RiskLabel} for item in allowed_raw)
    ):
        raise ValueError("statistical policy allowed_risk_labels is invalid")
    minimum_bootstrap = _finite_number(
        statistical.get("minimum_bootstrap_probability"),
        "minimum_bootstrap_probability",
    )
    if not 0 <= minimum_bootstrap < 1:
        raise ValueError("minimum_bootstrap_probability must be in [0, 1)")
    required_replays = external.get("required_replays")
    if type(required_replays) is not int or required_replays < 0:
        raise ValueError("external required_replays must be a nonnegative integer")
    external_drawdown = _finite_number(
        external.get("max_drawdown_floor"), "external max_drawdown_floor"
    )
    if not -1 <= external_drawdown <= 0:
        raise ValueError("external max_drawdown_floor must be in [-1, 0]")
    cost = mandate.cost_policy
    return MachineEvaluationPolicy(
        policy_id=mandate.mandate_id,
        policy_version="evaluation-mandate-v1",
        allowed_risk_labels=tuple(RiskLabel(item) for item in allowed_raw),
        minimum_bootstrap_probability=minimum_bootstrap,
        required_external_replays=required_replays,
        external_minimum_cagr=_finite_number(
            external.get("minimum_cagr"), "external minimum_cagr"
        ),
        external_max_drawdown_floor=external_drawdown,
        stress_minimum_cagr=_finite_number(cost["minimum_cagr"], "cost minimum_cagr"),
        stress_max_drawdown_floor=_finite_number(
            cost["max_drawdown_floor"], "cost max_drawdown_floor"
        ),
        stress_minimum_calmar=_finite_number(
            cost["minimum_calmar"], "cost minimum_calmar"
        ),
        blocking_stress_scenarios=tuple(cost["blocking_scenarios"]),
    )


def _external_replays(
    experiment: Path,
    snapshot: CandidateSnapshot,
) -> tuple[ExternalReplayEvidence, ...]:
    path = experiment / "artifacts" / "external_validation.json"
    if not path.is_file():
        return ()
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("external validation evidence must be an object")
    if document.get("candidate_id") != snapshot.candidate_id:
        raise ValueError("external validation candidate identity differs")
    if document.get("candidate_hash") != snapshot.candidate_hash:
        raise ValueError("external validation candidate hash differs")
    values = document.get("replays")
    if not isinstance(values, list):
        raise ValueError("external validation evidence has no replay list")
    replays: list[ExternalReplayEvidence] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("external validation replay must be an object")
        replays.append(
            ExternalReplayEvidence(
                replay_id=str(value["replay_id"]),
                symbol=str(value["symbol"]),
                candidate_id=snapshot.candidate_id,
                candidate_hash=snapshot.candidate_hash,
                dates=tuple(map(str, value["dates"])),
                returns=tuple(map(float, value["returns"])),
            )
        )
    return tuple(replays)


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


def _credential_view(credential: StrategyGovernanceCredential) -> dict[str, Any]:
    return {
        "credential_id": credential.credential_id,
        "strategy_id": credential.strategy_id,
        "stage": credential.stage.value,
        "result": credential.result.value,
        "credential_hash": credential.credential_hash,
        "seals": [seal.to_dict() for seal in credential.seals],
    }


def _latest_seal(
    credential: StrategyGovernanceCredential, stage: GovernanceStage
) -> StrategyGovernanceSeal:
    match = next((seal for seal in reversed(credential.seals) if seal.stage is stage), None)
    if match is None:
        raise ValueError(f"governance credential has no {stage.value} seal")
    return match


def _submission_from_credential(
    credential: StrategyGovernanceCredential,
) -> tuple[StrategyGovernanceSeal, CandidateSnapshot, EvaluationMandate, dict[str, Any]]:
    seal = _latest_seal(credential, GovernanceStage.CANDIDATE_SUBMITTED)
    try:
        snapshot = CandidateSnapshot.from_dict(dict(seal.content["candidate_snapshot"]))
        mandate = EvaluationMandate.from_dict(dict(seal.content["evaluation_mandate"]))
        audit_policy = dict(seal.content["audit_policy"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("candidate submission seal is incomplete") from exc
    if (
        snapshot.strategy_id != credential.strategy_id
        or mandate.strategy_id != credential.strategy_id
        or mandate.candidate_id != snapshot.candidate_id
    ):
        raise ValueError("candidate submission identities differ")
    expected_policy = {
        "policy_version": "tdr-freeze-v3",
        "required_audits": mandate.required_audits,
        "requirements": mandate.audit_requirements,
    }
    if audit_policy != expected_policy:
        raise ValueError("candidate submission audit policy differs from mandate")
    return seal, snapshot, mandate, audit_policy


def _adjudication_from_credential(
    credential: StrategyGovernanceCredential,
) -> tuple[StrategyGovernanceSeal, AdjudicationReport]:
    seal = _latest_seal(credential, GovernanceStage.TDR_ADJUDICATED)
    try:
        report = AdjudicationReport.from_dict(dict(seal.content["adjudication_report"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("TDR adjudication seal is incomplete") from exc
    return seal, report


def open_freeze_review(
    context: RepositoryContext,
    *,
    credential_id: str,
    candidate_path: Path,
    mandate_path: Path,
    actor: str,
    reason: str,
    runtime_root: Path | None = None,
    candidate_package: dict[str, Any] | None = None,
) -> CommandResult:
    """Human gate 2: freeze candidate, final mandate, and audit policy."""

    try:
        snapshot = CandidateSnapshot.from_dict(_read_object(context, candidate_path))
        mandate = EvaluationMandate.from_dict(_read_object(context, mandate_path))
        _validate_candidate_contract(snapshot)
        _validate_mandate_contract(mandate)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("candidate submission reason must be nonblank")
        unknown = sorted(set(mandate.required_audits) - KNOWN_AUDITS)
        if unknown:
            raise ValueError(f"evaluation mandate has unsupported audits: {unknown}")
        if mandate.finalized_by != actor:
            raise ValueError("evaluation mandate finalizer differs from gate actor")
        experiment = _experiment_path(context, snapshot.source_experiment)
        if experiment.name not in snapshot.source_experiment:
            raise ValueError("candidate source experiment identity is ambiguous")
        protocol, manifest, _candidate = _load_protocol_and_candidate(experiment, snapshot)
        _assert_mandate_alignment(protocol, manifest, mandate, snapshot)
        runtime = _candidate_runtime(snapshot, runtime_root)
        evaluation_inputs = _evaluation_inputs(experiment, protocol, manifest)
        registry = StrategyRegistry(context.strategy_root)
        credential = registry.get_governance_credential(snapshot.strategy_id, credential_id)
        audit_policy = {
            "policy_version": "tdr-freeze-v3",
            "required_audits": mandate.required_audits,
            "requirements": mandate.audit_requirements,
        }
        submission_content = {
            "submission_id": f"SUB-{credential_id}-{len(credential.seals) + 1}",
            "candidate_snapshot": snapshot.to_dict(),
            "evaluation_mandate": mandate.to_dict(),
            "audit_policy": audit_policy,
            "candidate_runtime": runtime,
            "evaluation_inputs": evaluation_inputs,
            "reason": reason.strip(),
        }
        if candidate_package is not None:
            submission_content["candidate_package"] = dict(candidate_package)
        submission_artifacts = {
            "candidate_snapshot": canonical_sha256(snapshot.to_dict()),
            "evaluation_mandate": mandate.mandate_hash,
            "audit_policy": canonical_sha256(audit_policy),
            "candidate_runtime": canonical_sha256(runtime),
            "evaluation_inputs": canonical_sha256(evaluation_inputs),
        }
        if candidate_package is not None:
            package_hash = candidate_package.get("package_hash")
            if not isinstance(package_hash, str) or len(package_hash) != 64:
                raise ValueError("candidate package has no valid package hash")
            submission_artifacts["candidate_package"] = package_hash
        last = credential.seals[-1]
        same_submission = (
            last.stage is GovernanceStage.CANDIDATE_SUBMITTED
            and last.actor == actor
            and last.content.get("candidate_snapshot") == snapshot.to_dict()
            and last.content.get("evaluation_mandate") == mandate.to_dict()
            and last.content.get("audit_policy") == audit_policy
            and last.content.get("candidate_runtime") == runtime
            and last.content.get("evaluation_inputs") == evaluation_inputs
            and last.content.get("reason") == reason.strip()
            and last.content.get("candidate_package") == candidate_package
            and last.artifact_hashes == submission_artifacts
        )
        if not same_submission:
            credential = registry.append_governance_seal(
                snapshot.strategy_id,
                credential_id,
                stage=GovernanceStage.CANDIDATE_SUBMITTED,
                result=GovernanceResult.OPEN,
                actor=actor,
                expected_previous_hash=credential.credential_hash,
                content=submission_content,
                artifact_hashes=submission_artifacts,
            )
    except (StrategyManagerError, StrategyRuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "freeze_review_open_failed",
            str(exc),
            context={
                "command": "strategy.review.open",
                "credential_id": credential_id,
            },
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.review.open",
        {"governance_credential": _credential_view(credential)},
    )


def show_freeze_review(
    context: RepositoryContext, strategy_id: str, credential_id: str
) -> CommandResult:
    try:
        registry = StrategyRegistry(context.strategy_root)
        credential = registry.get_governance_credential(strategy_id, credential_id)
        _seal, snapshot, mandate, _policy = _submission_from_credential(credential)
        try:
            _report_seal, adjudication = _adjudication_from_credential(credential)
            report = adjudication.to_dict()
        except ValueError:
            report = None
    except (StrategyManagerError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "freeze_review_read_failed",
            str(exc),
            context={
                "command": "strategy.review.show",
                "credential_id": credential_id,
            },
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.review.show",
        {
            "governance_credential": _credential_view(credential),
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
    snapshot: CandidateSnapshot,
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
    if str(manifest.get("symbol", "")).upper() != str(
        snapshot.data_contract["symbol"]
    ).upper():
        raise ValueError("evaluation symbol differs from candidate data contract")
    if str(manifest.get("asset_type", "etf")).lower() != str(
        snapshot.data_contract["asset_type"]
    ).lower():
        raise ValueError("evaluation asset type differs from candidate data contract")
    execution_hash = canonical_sha256(snapshot.execution_policy)
    if protocol.execution_policy_hash != execution_hash:
        raise ValueError("evaluation protocol execution policy differs from candidate")


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


def _assert_formal_evaluation_contract(
    evaluation: CommandResult,
    mandate: EvaluationMandate,
    snapshot: CandidateSnapshot,
    review_data_hash: str,
) -> None:
    actual = evaluation.result.get("formal_evaluation_contract")
    expected = {
        "metric_semantics_version": METRIC_SEMANTICS_VERSION,
        "review_data_hash": review_data_hash,
        "development_cutoff": mandate.development_cutoff,
        "windows": mandate.evaluation_windows,
        "benchmark_id": str(mandate.benchmark["id"]),
        "primary_fee_rate": float(mandate.cost_policy["primary_fee_rate"]),
        "stress_scenarios": list(mandate.cost_policy["stress_scenarios"]),
        "frequency_window_days": int(mandate.frequency_policy["window_days"]),
        "execution_policy_hash": canonical_sha256(snapshot.execution_policy),
        "execution_engine": EXECUTION_CONTRACT_VERSION,
        "artifact_reuse": False,
    }
    if actual != expected:
        raise ValueError("formal evaluation contract differs from evaluation mandate")


def _evaluation_inputs(experiment, protocol, manifest):
    return {
        "protocol": protocol.to_dict(), "manifest": manifest,
        "support_files": {
            name: _hash_file(experiment / "artifacts" / name)
            for name in ("external_validation.json", "monitoring_plan.json")
            if (experiment / "artifacts" / name).is_file()
        },
    }


def _assert_submitted_inputs(submission, experiment, protocol, manifest):
    current = _evaluation_inputs(experiment, protocol, manifest)
    if (submission.content.get("evaluation_inputs") != current
            or submission.artifact_hashes.get("evaluation_inputs") != canonical_sha256(current)):
        raise ValueError("evaluation inputs differ from the candidate submission seal")


def _review_directory(context, credential, submission):
    base = (context.root / "data" / "review").resolve()
    result = (base / credential.credential_id / submission.seal_hash).resolve()
    if result.parent.parent != base:
        raise ValueError("unsafe review snapshot identity")
    return result


def _review_experiment(directory, snapshot, source, protocol, manifest):
    """Copy definitions and human input evidence only; never reuse computed metrics."""
    target = directory / "experiments" / snapshot.strategy_id / source.name
    target.mkdir(parents=True, exist_ok=True)
    documents = {
        "evaluation_protocol.json": json.dumps(protocol.to_dict(), ensure_ascii=False, indent=2).encode(),
        protocol.candidate_manifest: json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
    }
    for name in ("external_validation.json", "monitoring_plan.json"):
        path = source / "artifacts" / name
        if path.is_file():
            documents["artifacts/" + name] = path.read_bytes()
    for name, raw in documents.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError(f"review input copy changed: {name}")
        else:
            with path.open("xb") as stream:
                stream.write(raw)
    return target


def _verified_review_evidence(context, credential, submission, adjudication, snapshot):
    directory = _review_directory(context, credential, submission)
    digest = adjudication.artifact_hashes.get("review_dataset")
    if not isinstance(digest, str):
        raise ValueError("adjudication has no sealed review dataset")
    verify_review_dataset(directory, digest)
    return directory / "experiments" / snapshot.strategy_id / Path(snapshot.source_experiment).name


def _number(value: Any) -> float:
    if value in {None, "", "None", "nan", "NaN"}:
        raise ValueError("metric is unavailable")
    return float(value)


def _claim_checks(snapshot: CandidateSnapshot, metric: dict[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for name, claim in snapshot.research_claims.items():
        metric_name = METRIC_ALIASES.get(name)
        if metric_name is None:
            checks.append({"claim": name, "status": "UNVERIFIABLE", "reason": "unsupported metric"})
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


def _frequency_check(
    mandate: EvaluationMandate,
    metric: dict[str, str],
) -> dict[str, Any]:
    policy = mandate.frequency_policy
    try:
        window_days = int(metric["frequency_window_days"])
        median = _number(metric["rolling_closed_trades_median"])
        p10 = _number(metric["rolling_closed_trades_p10"])
    except (KeyError, ValueError) as exc:
        return {
            "status": "INCOMPLETE",
            "reason": f"frequency evidence is unavailable: {exc}",
        }
    if window_days != int(policy["window_days"]):
        return {
            "status": "FAIL",
            "reason": "frequency window differs from mandate",
            "window_days": window_days,
        }
    status = "PASS"
    minimum = policy.get("minimum_closed_trades")
    if policy["mode"] == "HARD" and median < int(minimum):
        status = "FAIL"
    return {
        "status": status,
        "mode": policy["mode"],
        "window_days": window_days,
        "rolling_closed_trades_median": median,
        "rolling_closed_trades_p10": p10,
        "minimum_closed_trades": minimum,
    }


def _candidate_runtime(
    snapshot: CandidateSnapshot, runtime_root: Path | None = None,
) -> dict[str, Any]:
    report = validate_candidate_readiness(snapshot, source_root=runtime_root)
    audit = _runtime_audit(snapshot, report)
    if audit.get("status") != "PASS":
        raise ValueError(f"candidate runtime contract mismatch: {audit.get('reason')}")
    return report


def _submitted_runtime(
    submission: StrategyGovernanceSeal, snapshot: CandidateSnapshot,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    current = _candidate_runtime(snapshot, runtime_root)
    if (
        submission.content.get("candidate_runtime") != current
        or submission.artifact_hashes.get("candidate_runtime") != canonical_sha256(current)
    ):
        raise ValueError("candidate runtime differs from its submission seal")
    return current


def _prospective_runtime(
    registry: StrategyRegistry,
    snapshot: CandidateSnapshot,
    mandate: EvaluationMandate,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    versions = registry.versions(snapshot.strategy_id)
    version = f"v{len(versions) + 1}"
    placeholder_governance = {
        "credential_id": "PROSPECTIVE",
        "candidate_submission_seal_hash": "0" * 64,
        "adjudication_seal_hash": "0" * 64,
        "approval_seal_hash": "0" * 64,
        "candidate_snapshot_hash": "0" * 64,
        "evaluation_mandate_hash": "0" * 64,
        "adjudication_report_hash": "0" * 64,
    }
    draft = StrategyVersion.from_dict(
        {
            "schema_version": 3,
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
    report = validate_runtime_readiness(draft, source_root=runtime_root)
    report["strategy_payload_hash"] = canonical_sha256(snapshot.strategy_payload)
    return report


def _runtime_audit(snapshot: CandidateSnapshot, runtime: dict[str, Any]) -> dict[str, Any]:
    if runtime.get("status") != "PASS":
        return {"status": "FAIL", "reason": "runtime readiness did not pass"}
    runtime_inputs = runtime.get("input_contract")
    expected_inputs = {"requirements": snapshot.data_contract["requirements"]}
    if runtime_inputs != expected_inputs:
        return {
            "status": "FAIL",
            "reason": "runtime input contract differs from candidate data contract",
            "expected_hash": canonical_sha256(expected_inputs),
            "actual_hash": canonical_sha256(runtime_inputs),
        }
    if runtime.get("execution_policy") != snapshot.execution_policy:
        return {
            "status": "FAIL",
            "reason": "runtime execution policy differs from candidate",
            "expected_hash": canonical_sha256(snapshot.execution_policy),
            "actual_hash": canonical_sha256(runtime.get("execution_policy")),
        }
    return {
        "status": "PASS",
        "runtime_sha256": runtime["runtime_sha256"],
        "strategy_payload_hash": runtime["strategy_payload_hash"],
        "input_contract_sha256": runtime["input_contract_sha256"],
        "execution_policy_sha256": runtime["execution_policy_sha256"],
        "evidence_hash": canonical_sha256(runtime),
    }


def _artifact_audit(
    experiment: Path,
    audit: str,
    machine: dict[str, Any],
    requirement: dict[str, Any],
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
        if audit == "monitoring_plan":
            rules = document.get("rules")
            if document.get("status") != requirement["required_status"]:
                return {"status": "FAIL", "artifact": mapping[audit], "detail": document}
            if not isinstance(rules, list) or len(rules) < int(requirement["minimum_rules"]):
                return {
                    "status": "INCOMPLETE",
                    "artifact": mapping[audit],
                    "reason": "monitoring plan has too few structured rules",
                }
    machine_checks = {
        "parameter_robustness": "parameter_robustness",
        "statistical_robustness": "statistical_robustness",
        "cost_stress": "cost_stress",
        "external_validation": "external_reproduction",
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
        if (
            audit == "external_validation"
            and status == "NOT_APPLICABLE"
            and int(requirement["required_replays"]) == 0
        ):
            normalized = "PASS"
        metrics = dict(check.get("metrics", {}))
        if audit == "parameter_robustness" and normalized == "PASS":
            if int(metrics.get("valid_neighbor_count", 0)) < int(
                requirement["minimum_valid_neighbors"]
            ):
                normalized = "FAIL"
        if audit == "statistical_robustness" and normalized == "PASS":
            if float(metrics.get("pbo", 1.0)) > float(requirement["maximum_pbo"]):
                normalized = "FAIL"
            if float(metrics.get("dsr_effective_probability", 0.0)) < float(
                requirement["minimum_dsr_probability"]
            ):
                normalized = "FAIL"
        return {
            "status": normalized,
            "artifact": mapping[audit],
            "machine_status": status,
            "reason_codes": list(check.get("reason_codes", [])),
            "metrics": metrics,
            "evidence_hash": _hash_file(path),
        }
    return {
        "status": "PASS",
        "artifact": mapping[audit],
        "evidence_hash": _hash_file(path),
    }


def evaluate_freeze_review(
    context: RepositoryContext,
    strategy_id: str,
    credential_id: str,
    *,
    runtime_root: Path | None = None,
    chart_descriptor: dict[str, object] | None = None,
) -> CommandResult:
    """Independently recompute the candidate and issue one immutable report."""

    registry = StrategyRegistry(context.strategy_root)
    try:
        credential = registry.get_governance_credential(strategy_id, credential_id)
        if credential.stage is GovernanceStage.TDR_ADJUDICATED:
            submission, snapshot, _mandate, _policy = _submission_from_credential(credential)
            adjudication, report = _adjudication_from_credential(credential)
            experiment = _verified_review_evidence(context, credential, submission, adjudication, snapshot)
            if credential.result is GovernanceResult.ELIGIBLE:
                runtime = _submitted_runtime(submission, snapshot, runtime_root)
                _verify_adjudication_evidence(experiment, report, runtime)
            return CommandResult(
                "PASS",
                "strategy.review.evaluate",
                {
                    "governance_credential": _credential_view(credential),
                    "adjudication_report": report.to_dict(),
                    "idempotent_replay": True,
                },
                {"source_experiment": str(experiment.relative_to(context.root))},
            )
        if credential.stage is not GovernanceStage.CANDIDATE_SUBMITTED:
            raise ValueError(f"governance credential cannot be evaluated: {credential.stage.value}")
        submission, snapshot, mandate, audit_policy = _submission_from_credential(credential)
        experiment = _experiment_path(context, snapshot.source_experiment)
        protocol, manifest, _candidate = _load_protocol_and_candidate(experiment, snapshot)
        _validate_candidate_contract(snapshot)
        _validate_mandate_contract(mandate)
        _assert_mandate_alignment(protocol, manifest, mandate, snapshot)
        runtime = _submitted_runtime(submission, snapshot, runtime_root)
        _assert_submitted_inputs(submission, experiment, protocol, manifest)
        source_experiment = experiment
        review_directory = _review_directory(context, credential, submission)
        dataset = publish_review_dataset(
            context, {**manifest, "strategy_id": snapshot.strategy_id}, protocol, review_directory,
        )
        experiment = _review_experiment(
            review_directory, snapshot, source_experiment, protocol, manifest,
        )
        evaluation_context = replace(context, experiments_root=review_directory / "experiments")
        machine_policy = _machine_policy_from_mandate(mandate)
        evaluation = evaluate_experiment(
            evaluation_context,
            experiment.name,
            use_cached_result=False,
            allow_artifact_reuse=False,
            machine_policy=machine_policy,
            stress_scenarios=tuple(mandate.cost_policy["stress_scenarios"]),
            frequency_window_days=int(mandate.frequency_policy["window_days"]),
            external_replays=_external_replays(experiment, snapshot),
            review_data_root=review_directory,
            review_data_hash=dataset["snapshot_hash"],
            candidate_runtime_roots={
                snapshot.candidate_id: runtime_root,
                f"{snapshot.strategy_id}-{snapshot.candidate_id}": runtime_root,
            }
            if runtime_root is not None
            else None,
            candidate_chart_descriptors={
                snapshot.candidate_id: chart_descriptor,
                f"{snapshot.strategy_id}-{snapshot.candidate_id}": chart_descriptor,
            }
            if chart_descriptor is not None
            else None,
        )
        machine = evaluation.result.get("machine_evaluation")
        if not isinstance(machine, dict):
            raise ValueError("formal evaluation produced no machine report")
        _assert_formal_evaluation_contract(evaluation, mandate, snapshot, dataset["snapshot_hash"])
        ranking = evaluation.result.get("ranking")
        if not isinstance(ranking, dict) or ranking.get("champion_id") != snapshot.candidate_id:
            raise ValueError("formal evaluation selected another candidate or found no unique champion")
        metric = _formal_metric(experiment, snapshot.candidate_id)
        claims = _claim_checks(snapshot, metric)
        objectives = _objective_checks(mandate, metric)
        audit_results: dict[str, dict[str, Any]] = {}
        for audit in mandate.required_audits:
            if audit == "objective_recalculation":
                status = "PASS" if all(item["status"] == "PASS" for item in objectives) else "FAIL"
                audit_results[audit] = {
                    "status": status,
                    "checks": objectives,
                    "artifact": "formal_metrics.csv",
                    "evidence_hash": _hash_file(experiment / "artifacts" / "formal_metrics.csv"),
                }
            elif audit == "frequency_recalculation":
                frequency = _frequency_check(mandate, metric)
                frequency["evidence_hash"] = _hash_file(
                    experiment / "artifacts" / "formal_metrics.csv"
                )
                frequency["artifact"] = "formal_metrics.csv"
                audit_results[audit] = frequency
            elif audit == "runtime_acceptance":
                audit_results[audit] = _runtime_audit(snapshot, runtime)
            elif audit == "technical_replay":
                metric_hash = evaluation.result.get("canonical_metric_hash")
                if not isinstance(metric_hash, str) or len(metric_hash) != 64:
                    audit_results[audit] = {
                        "status": "FAIL",
                        "reason": "independent replay produced no canonical metric hash",
                    }
                else:
                    requirement = mandate.audit_requirements["technical_replay"]
                    engine = evaluation.result["formal_evaluation_contract"].get(
                        "execution_engine"
                    )
                    engine_matches = engine == requirement["execution_engine"]
                    audit_results[audit] = {
                        "status": "PASS" if engine_matches else "FAIL",
                        "mode": "FULL_RECOMPUTE_WITHOUT_ARTIFACT_REUSE",
                        "required_mode": requirement["mode"],
                        "artifact_reuse": False,
                        "execution_engine": engine,
                        "required_execution_engine": requirement["execution_engine"],
                        "canonical_metric_hash": metric_hash,
                        "artifact": "evaluation_result.json",
                        "artifact_hash": _hash_file(
                            experiment / "artifacts" / "evaluation_result.json"
                        ),
                        "evidence_hash": canonical_sha256(
                            {
                                "input_hash": evaluation.result.get("input_hash"),
                                "decision_hash": evaluation.result.get("decision_hash"),
                                "canonical_metric_hash": metric_hash,
                            }
                        ),
                    }
            else:
                requirement = (
                    mandate.cost_policy
                    if audit == "cost_stress"
                    else mandate.audit_requirements[audit]
                )
                audit_results[audit] = _artifact_audit(
                    experiment, audit, machine, requirement
                )
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
            "report_id": f"ADR-{credential_id}-{submission.sequence}",
            "review_id": credential_id,
            "strategy_id": strategy_id,
            "candidate_id": snapshot.candidate_id,
            "candidate_hash": snapshot.candidate_hash,
            "evaluation_mandate_hash": mandate.mandate_hash,
            "audit_policy_hash": canonical_sha256(audit_policy),
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
        result = {
            "ELIGIBLE_FOR_FREEZE_REVIEW": GovernanceResult.ELIGIBLE,
            "INCOMPLETE": GovernanceResult.INCOMPLETE,
            "REJECTED": GovernanceResult.REJECTED,
        }[report.machine_verdict]
        _submitted_runtime(submission, snapshot, runtime_root)
        _assert_submitted_inputs(submission, experiment, protocol, manifest)
        verify_review_dataset(review_directory, dataset["snapshot_hash"])
        updated = registry.append_governance_seal(
            strategy_id,
            credential_id,
            stage=GovernanceStage.TDR_ADJUDICATED,
            result=result,
            actor="TDR",
            expected_previous_hash=credential.credential_hash,
            content={
                "submission_seal_hash": submission.seal_hash,
                "adjudication_report": report.to_dict(),
            },
            artifact_hashes={
                "adjudication_report": report.report_hash,
                "review_dataset": dataset["snapshot_hash"],
                "source_experiment": canonical_sha256(
                    {
                        "path": str(experiment.relative_to(context.root)).replace("\\", "/"),
                        "canonical_metric_hash": evaluation.result.get("canonical_metric_hash"),
                    }
                ),
            },
        )
    except (StrategyManagerError, StrategyRuntimeError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "freeze_review_evaluation_failed",
            str(exc),
            context={
                "command": "strategy.review.evaluate",
                "credential_id": credential_id,
            },
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.review.evaluate",
        {
            "governance_credential": _credential_view(updated),
            "adjudication_report": report.to_dict(),
        },
        {"source_experiment": str(experiment.relative_to(context.root))},
    )


def _verify_adjudication_evidence(
    experiment: Path,
    report: AdjudicationReport,
    runtime: dict[str, Any],
) -> None:
    """Reject a freeze when evidence changed after the TDR adjudication seal."""

    for audit, result in report.audit_results.items():
        if result.get("status") != "PASS":
            raise ValueError(f"adjudication audit is not PASS: {audit}")
        if audit == "runtime_acceptance":
            if result.get("evidence_hash") != canonical_sha256(runtime):
                raise ValueError("runtime acceptance changed after adjudication")
            continue
        artifact = result.get("artifact")
        if not isinstance(artifact, str):
            raise ValueError(f"adjudication audit has no immutable artifact: {audit}")
        path = experiment / "artifacts" / artifact
        if not path.is_file():
            raise ValueError(f"adjudication evidence disappeared: {artifact}")
        expected = (
            result.get("artifact_hash")
            if audit == "technical_replay"
            else result.get("evidence_hash")
        )
        if expected != _hash_file(path):
            raise ValueError(f"adjudication evidence changed after review: {artifact}")


def freeze_review_candidate(
    context: RepositoryContext,
    strategy_id: str,
    credential_id: str,
    *,
    actor: str,
    reason: str,
    change_summary: str,
    runtime_root: Path | None = None,
) -> CommandResult:
    """Human gate 3: revalidate runtime and atomically create the frozen version."""

    registry = StrategyRegistry(context.strategy_root)
    try:
        credential = registry.get_governance_credential(strategy_id, credential_id)
        if credential.stage is GovernanceStage.VERSION_FROZEN:
            version, event, credential = registry.create_frozen_version_from_credential(
                strategy_id,
                credential_id,
                actor=actor,
                reason=reason,
                evidence={},
                change_summary=change_summary,
            )
            return CommandResult(
                "PASS",
                "strategy.freeze",
                {
                    "version": version.to_dict(),
                    "event": event.to_dict(),
                    "governance_credential": _credential_view(credential),
                    "pte_deployment": "NOT_REQUESTED",
                    "idempotent_replay": True,
                },
            )
        if credential.stage not in {
            GovernanceStage.TDR_ADJUDICATED,
            GovernanceStage.FREEZE_APPROVED,
        }:
            raise ValueError("governance credential does not contain an eligible TDR adjudication")
        if (
            credential.stage is GovernanceStage.TDR_ADJUDICATED
            and credential.result is not GovernanceResult.ELIGIBLE
        ):
            raise ValueError("governance credential does not contain an eligible TDR adjudication")
        submission, snapshot, mandate, _audit_policy = _submission_from_credential(credential)
        adjudication, report = _adjudication_from_credential(credential)
        candidate_runtime = _submitted_runtime(submission, snapshot, runtime_root)
        experiment = _verified_review_evidence(context, credential, submission, adjudication, snapshot)
        protocol, manifest, _candidate = _load_protocol_and_candidate(experiment, snapshot)
        _assert_submitted_inputs(submission, experiment, protocol, manifest)
        _assert_mandate_alignment(protocol, manifest, mandate, snapshot)
        _verify_adjudication_evidence(experiment, report, candidate_runtime)
        runtime = _prospective_runtime(registry, snapshot, mandate, runtime_root)
        runtime["strategy_payload_hash"] = canonical_sha256(snapshot.strategy_payload)
        runtime_audit = _runtime_audit(snapshot, runtime)
        if runtime_audit.get("status") != "PASS":
            raise ValueError(
                f"runtime acceptance differs from candidate: {runtime_audit.get('reason')}"
            )
        require_same_runtime_content(candidate_runtime, runtime)
        if credential.stage is GovernanceStage.TDR_ADJUDICATED:
            human_decision = {
                "credential_id": credential_id,
                "strategy_id": strategy_id,
                "candidate_id": snapshot.candidate_id,
                "candidate_hash": snapshot.candidate_hash,
                "decision": "APPROVE_FREEZE",
                "actor": actor,
                "reason": reason,
                "decided_at": _now(),
            }
            credential = registry.append_governance_seal(
                strategy_id,
                credential_id,
                stage=GovernanceStage.FREEZE_APPROVED,
                result=GovernanceResult.APPROVED,
                actor=actor,
                expected_previous_hash=credential.credential_hash,
                content={
                    "adjudication_report_hash": report.report_hash,
                    "human_decision": human_decision,
                    "runtime_acceptance": runtime,
                },
                artifact_hashes={
                    "human_decision": canonical_sha256(human_decision),
                    "runtime_acceptance": canonical_sha256(runtime),
                },
            )
        else:
            approval = credential.seals[-1]
            if (
                approval.content.get("runtime_acceptance") != runtime
                or approval.artifact_hashes.get("runtime_acceptance")
                != canonical_sha256(runtime)
            ):
                raise ValueError("approved runtime acceptance changed before freeze")
        metric = _formal_metric(experiment, snapshot.candidate_id)
        evidence = {
            "schema_version": 1,
            "evidence_id": f"EVD-{strategy_id}-{snapshot.candidate_id}-{credential_id}",
            "strategy_id": strategy_id,
            "version": "v1",
            "release_hash": "0" * 64,
            "phase": "RESEARCH_BACKTEST",
            "period_start": str(mandate.evaluation_windows["full"]["start"]),
            "period_end": str(mandate.evaluation_windows["full"]["end"]),
            "data_identity": {
                "governance_credential_id": credential_id,
                "candidate_snapshot_hash": canonical_sha256(snapshot.to_dict()),
                "evaluation_mandate_hash": mandate.mandate_hash,
                "review_dataset_hash": adjudication.artifact_hashes["review_dataset"],
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
        version, event, credential = registry.create_frozen_version_from_credential(
            strategy_id,
            credential_id,
            actor=actor,
            reason=reason,
            evidence=evidence,
            change_summary=change_summary,
        )
    except (StrategyManagerError, StrategyRuntimeError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "strategy_freeze_failed",
            str(exc),
            context={
                "command": "strategy.freeze",
                "credential_id": credential_id,
            },
        ) from exc
    return CommandResult(
        "PASS",
        "strategy.freeze",
        {
            "version": version.to_dict(),
            "event": event.to_dict(),
            "governance_credential": _credential_view(credential),
            "pte_deployment": "NOT_REQUESTED",
        },
    )
