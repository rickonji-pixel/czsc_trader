"""Trader orchestration for immutable strategy-evaluation bundles."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
from czsc_trader.temp_workspace import create_temporary_directory
from strategy_evaluator import (
    CandidateDescriptor,
    EvaluationProtocol,
    MachineEvaluationCase,
    MachineEvaluationPolicy,
    MetricObservation,
    RiskLabel,
    TrialRecord,
    compare_observation,
    evaluate_machine_eligibility,
    finalize_evaluation,
    legacy_health_evidence,
    rank_candidates,
    render_summary,
    required_stress_scenarios,
    resolve_margins,
    screen_candidates,
    validate_protocol,
)

from czsc_trader.application.evaluation_evidence import build_champion_audit_request
from czsc_trader.candidate_evaluation import (
    CandidateEvaluationContext,
    _scenario_settings,
    evaluate_candidate_payloads,
)
from czsc_trader.evaluation_artifacts import (
    EvaluationIdentity,
    ReuseLedgerRow,
    load_reusable_observations,
)
from czsc_trader.experiment_archive import resolve_experiment_dir
from czsc_trader.identity import canonical_json_sha256

from .context import RepositoryContext
from .results import CommandResult

Runner = Callable[..., tuple[MetricObservation, ...]]
SCREENING_AUDIT_FILES = (
    "screening_metrics.csv",
    "screening_noninferiority.csv",
    "screening_decisions.csv",
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _canonical_hash(*values: object) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_screening_audit(artifact_dir: Path, result: dict[str, Any]) -> None:
    hashes = result.get("audit_artifacts")
    if not isinstance(hashes, dict):
        raise ValueError("completed evaluation is missing screening audit hashes")
    missing_required = [name for name in SCREENING_AUDIT_FILES if name not in hashes]
    if missing_required:
        raise ValueError(f"completed evaluation is missing screening audit hashes: {missing_required}")
    for name, expected in hashes.items():
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("completed evaluation contains invalid audit artifact name")
        path = artifact_dir / name
        if not path.is_file():
            raise ValueError(f"completed evaluation is missing screening audit: {name}")
        actual = _text_hash(path.read_text(encoding="utf-8"))
        if expected != actual:
            raise ValueError(f"completed evaluation audit artifact hash mismatch: {name}")


def _experiment_path(context: RepositoryContext, experiment_id: str) -> Path:
    return resolve_experiment_dir(context.experiments_root, experiment_id)


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


def _execution_settings(manifest: dict[str, Any]) -> tuple[int, bool, tuple[str, ...]]:
    workers = manifest.get("evaluation_workers", 1)
    if type(workers) is not int or workers <= 0:
        raise ValueError("evaluation_workers must be a positive integer")
    reuse = manifest.get("reuse_experiment_artifacts", False)
    if type(reuse) is not bool:
        raise ValueError("reuse_experiment_artifacts must be a boolean")
    raw_sources = manifest.get("reuse_source_experiments", [])
    if not isinstance(raw_sources, list) or any(
        not isinstance(item, str) or not item or Path(item).name != item
        for item in raw_sources
    ):
        raise ValueError("reuse_source_experiments must contain experiment IDs")
    sources = tuple(raw_sources)
    if reuse and not sources:
        raise ValueError("reuse_source_experiments is required when reuse is enabled")
    if sources and not reuse:
        raise ValueError("reuse_experiment_artifacts must be enabled when reuse sources are set")
    return workers, reuse, sources


def _requested_identities(
    protocol: EvaluationProtocol,
    manifest: dict[str, Any],
    candidates: dict[str, dict[str, object]],
    candidate_ids: tuple[str, ...],
    tier: str,
    scenarios: tuple[str, ...],
) -> tuple[EvaluationIdentity, ...]:
    source_files = manifest.get("source_files")
    if not isinstance(source_files, dict):
        raise ValueError("artifact reuse requires candidate manifest source_files")
    windows = manifest.get("windows")
    if not isinstance(windows, dict):
        raise ValueError("candidate manifest requires windows")
    base_fee = float(manifest.get("fee_rate", 0.0005))
    semantics = str(manifest.get("metric_semantics_version", "candidate-metrics-v1"))
    data_identity = canonical_json_sha256(source_files)
    identities: list[EvaluationIdentity] = []
    for candidate_id in candidate_ids:
        candidate = candidates[candidate_id]
        for scenario in scenarios:
            fee_rate, _ = _scenario_settings(scenario, base_fee)
            for window_id, value in windows.items():
                if not isinstance(value, dict):
                    continue
                identities.append(EvaluationIdentity(
                    candidate_id=candidate_id,
                    candidate_hash=str(candidate.get("strategy_hash", candidate.get("candidate_hash", ""))),
                    execution_policy_hash=str(candidate["execution_policy_hash"]),
                    data_identity=data_identity,
                    development_cutoff=str(protocol.development_cutoff),
                    window_id=str(window_id),
                    window_start=str(value["start"]),
                    window_end=str(value["end"]),
                    tier=tier,
                    scenario_id=scenario,
                    fee_rate=fee_rate,
                    metric_semantics_version=semantics,
                ))
    return tuple(identities)


def _run_with_reuse(
    runner: Runner,
    run_context: CandidateEvaluationContext,
    protocol: EvaluationProtocol,
    payloads: tuple[dict[str, object], ...],
    candidate_ids: tuple[str, ...],
    tier: str,
    scenarios: tuple[str, ...],
    manifest: dict[str, Any],
    source_ids: tuple[str, ...],
    ledger: list[ReuseLedgerRow],
    diagnostics: list[str],
) -> tuple[MetricObservation, ...]:
    by_candidate = {str(item["candidate_id"]): item for item in payloads}
    identities = _requested_identities(
        protocol, manifest, by_candidate, candidate_ids, tier, scenarios,
    )
    reused = load_reusable_observations(
        run_context.repository.experiments_root, source_ids, identities,
    )
    diagnostics.extend(reused.diagnostics)
    hit_keys = {item.evaluation_key for item in reused.ledger}
    identities_by_candidate = {
        candidate_id: tuple(item for item in identities if item.candidate_id == candidate_id)
        for candidate_id in candidate_ids
    }
    reusable_ids = {
        candidate_id
        for candidate_id, values in identities_by_candidate.items()
        if values and all(item.key in hit_keys for item in values)
    }
    reused_observations = tuple(
        item for item in reused.observations if item.candidate_id in reusable_ids
    )
    ledger.extend(
        item for item in reused.ledger if item.candidate_id in reusable_ids
    )
    missing_ids = tuple(item for item in candidate_ids if item not in reusable_ids)
    computed = runner(
        run_context, protocol, payloads, missing_ids, tier, scenarios,
    ) if missing_ids else ()
    identity_by_coordinates = {
        (item.candidate_id, item.window_id, item.scenario_id, item.tier): item
        for item in identities
    }
    for item in computed:
        identity = identity_by_coordinates[
            (item.candidate_id, item.window_id, item.scenario_id, item.measurement_tier)
        ]
        ledger.append(ReuseLedgerRow(
            identity.key, item.candidate_id, item.window_id, item.scenario_id,
            item.measurement_tier, "COMPUTED",
        ))
    observations = {
        (item.candidate_id, item.window_id, item.scenario_id, item.measurement_tier): item
        for item in (*reused_observations, *computed)
    }
    return tuple(
        observations[(item.candidate_id, item.window_id, item.scenario_id, item.tier)]
        for item in identities
    )


def _csv_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _return_matrix_csv(evidence) -> str:
    return _csv_text([
        {"date": date_value, **dict(zip(evidence.candidate_ids, row, strict=True))}
        for date_value, row in zip(evidence.dates, evidence.returns, strict=True)
    ])


def _applicable_comparisons(
    protocol: EvaluationProtocol,
    challenger: MetricObservation,
    incumbent: MetricObservation,
    margins,
):
    values = compare_observation(challenger, incumbent, margins)
    if challenger.window_id != "full" and challenger.window_id not in protocol.target_windows:
        values = tuple(item for item in values if item.metric != "profit_factor")
    return values


def _screening_audit(
    protocol: EvaluationProtocol,
    candidates: tuple[CandidateDescriptor, ...],
    screening: tuple[MetricObservation, ...],
    shortlist,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    representatives: dict[str, CandidateDescriptor] = {}
    duplicate_of: dict[str, str] = {}
    challengers = sorted((item for item in candidates if not item.is_incumbent), key=lambda item: item.candidate_id)
    for item in challengers:
        behavior = item.behavior_hash or item.candidate_hash
        representative = representatives.get(behavior)
        if representative is None:
            representatives[behavior] = item
        else:
            duplicate_of[item.candidate_id] = representative.candidate_id

    by_key = {
        (item.candidate_id, item.window_id): item
        for item in screening
        if item.scenario_id == "standard"
    }
    selected = set(shortlist.candidate_ids)
    margins = resolve_margins(protocol)
    comparison_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for item in challengers:
        duplicate = duplicate_of.get(item.candidate_id)
        if duplicate is not None:
            decisions.append({
                "candidate_id": item.candidate_id,
                "outcome": "BEHAVIOR_DEDUPLICATED",
                "representative_candidate_id": duplicate,
                "failed_count": 0,
                "reason_codes": "BEHAVIOR_DEDUPLICATED",
            })
            continue
        failures: list[str] = []
        for window in protocol.decision_windows:
            challenger = by_key.get((item.candidate_id, window))
            incumbent = by_key.get((protocol.incumbent_id, window))
            if challenger is None or incumbent is None:
                failures.append(f"MISSING_WINDOW_{window.upper()}")
                continue
            for comparison in _applicable_comparisons(protocol, challenger, incumbent, margins):
                comparison_rows.append({"candidate_id": item.candidate_id, **comparison.to_dict()})
                if not comparison.passed:
                    failures.append(comparison.reason_code)
        if item.candidate_id in selected:
            outcome = "SHORTLISTED"
        elif failures:
            outcome = "SCREENING_NONINFERIORITY"
        else:
            outcome = "SHORTLIST_LIMIT"
        decisions.append({
            "candidate_id": item.candidate_id,
            "outcome": outcome,
            "representative_candidate_id": "",
            "failed_count": len(failures),
            "reason_codes": ";".join(dict.fromkeys(failures)),
        })
    return comparison_rows, decisions


def evaluate_experiment(
    context: RepositoryContext,
    experiment_id: str,
    *,
    runner: Runner = evaluate_candidate_payloads,
    use_cached_result: bool = True,
    allow_artifact_reuse: bool = True,
) -> CommandResult:
    experiment = _experiment_path(context, experiment_id)
    protocol_raw = _read_object(experiment / "evaluation_protocol.json")
    protocol = EvaluationProtocol.from_dict(protocol_raw)
    if protocol.experiment_id != experiment_id:
        raise ValueError("protocol experiment_id does not match directory")
    manifest_path = (experiment / protocol.candidate_manifest).resolve()
    if manifest_path.parent != experiment or not manifest_path.is_file():
        raise ValueError("candidate manifest must be a direct experiment child")
    manifest = _read_object(manifest_path)
    workers, reuse, reuse_sources = _execution_settings(manifest)
    if not allow_artifact_reuse:
        reuse = False
        reuse_sources = ()
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
    if result_path.is_file() and use_cached_result:
        existing = _read_object(result_path)
        if existing.get("input_hash") != input_hash:
            raise ValueError("completed evaluation has a different input hash")
        stored_hash = existing.pop("decision_hash", None)
        if stored_hash != _canonical_hash(existing):
            raise ValueError("completed evaluation decision hash mismatch")
        existing["decision_hash"] = stored_hash
        _validate_screening_audit(artifact_dir, existing)
        return CommandResult("PASS", "strategy.evaluate", existing, {"directory": str(artifact_dir)})

    windows_raw = manifest.get("windows")
    if not isinstance(windows_raw, dict) or not windows_raw:
        raise ValueError("candidate manifest requires windows")
    periods = tuple((str(name), (pd.Timestamp(value["start"]), pd.Timestamp(value["end"]))) for name, value in windows_raw.items() if isinstance(value, dict))
    run_context = CandidateEvaluationContext(
        context, str(manifest["symbol"]), str(manifest.get("asset_type", "etf")), periods,
        float(manifest.get("fee_rate", 0.0005)), float(manifest.get("init_cash", 1_000_000.0)),
        workers,
    )
    reuse_ledger: list[ReuseLedgerRow] = []
    reuse_diagnostics: list[str] = []

    def run_tier(
        candidate_ids: tuple[str, ...],
        tier: str,
        scenarios: tuple[str, ...] = ("standard",),
        *,
        allow_reuse: bool = True,
    ) -> tuple[MetricObservation, ...]:
        if not reuse:
            return runner(run_context, protocol, payloads, candidate_ids, tier, scenarios)
        if not allow_reuse:
            computed = runner(run_context, protocol, payloads, candidate_ids, tier, scenarios)
            by_candidate = {str(item["candidate_id"]): item for item in payloads}
            identities = _requested_identities(
                protocol, manifest, by_candidate, candidate_ids, tier, scenarios,
            )
            identity_by_coordinates = {
                (item.candidate_id, item.window_id, item.scenario_id, item.tier): item
                for item in identities
            }
            for item in computed:
                identity = identity_by_coordinates[
                    (item.candidate_id, item.window_id, item.scenario_id, item.measurement_tier)
                ]
                reuse_ledger.append(ReuseLedgerRow(
                    identity.key, item.candidate_id, item.window_id, item.scenario_id,
                    item.measurement_tier, "COMPUTED",
                ))
            return computed
        return _run_with_reuse(
            runner, run_context, protocol, payloads, candidate_ids, tier, scenarios,
            manifest, reuse_sources, reuse_ledger, reuse_diagnostics,
        )

    preliminary = screen_candidates(protocol, candidates, ())
    screening_ids = (protocol.incumbent_id, *preliminary.candidate_ids)
    screening = run_tier(screening_ids, "SCREENING")
    validate_protocol(protocol, candidates, screening, trials)
    screening_ranking = rank_candidates(protocol, preliminary, screening, candidates)
    shortlist = screen_candidates(protocol, candidates, screening)
    screening_comparisons, screening_decisions = _screening_audit(protocol, candidates, screening, shortlist)
    formal_ids = (protocol.incumbent_id, *shortlist.candidate_ids)
    formal = run_tier(formal_ids, "FORMAL")
    validate_protocol(protocol, candidates, formal, trials)
    ranking = rank_candidates(protocol, shortlist, formal, candidates)
    health = None
    champion_audit = None
    machine_report = None
    audit_request = None
    stress: tuple[MetricObservation, ...] = ()
    repeated: tuple[MetricObservation, ...] = ()
    if ranking.champion_id:
        pair = (protocol.incumbent_id, ranking.champion_id)
        scenarios = (
            tuple(item.scenario_id for item in required_stress_scenarios())
            if protocol.standard_version == "opc-v3" else ("fee_x2",)
        )
        stress = run_tier(pair, "STRESS", scenarios)
        validate_protocol(protocol, candidates, stress, trials)
        repeated = run_tier((ranking.champion_id,), "FORMAL", allow_reuse=False)
        if protocol.standard_version == "opc-v3":
            audit_request = build_champion_audit_request(
                run_context=run_context, protocol=protocol, manifest=manifest,
                payloads=payloads, candidates=candidates, trials=trials, ranking=ranking,
                screening_profiles=screening_ranking.profiles, formal=formal,
                repeated=repeated, stress=stress,
                search_candidate_ids=preliminary.candidate_ids,
            )
            champion = next(
                item for item in candidates if item.candidate_id == ranking.champion_id
            )
            machine_report = evaluate_machine_eligibility(MachineEvaluationCase(
                report_id=f"SE-{experiment_id}-{ranking.champion_id}",
                candidate_hash=champion.candidate_hash,
                policy=MachineEvaluationPolicy(
                    policy_id="OPC-MACHINE-ELIGIBILITY",
                    policy_version="v1",
                    allowed_risk_labels=(RiskLabel.FAVORABLE, RiskLabel.MIXED),
                    blocking_stress_scenarios=("total_cost_15bp",),
                ),
                audit_request=audit_request,
            ))
            champion_audit = machine_report.audit_result
        else:
            health = legacy_health_evidence(
                protocol, ranking, formal, stress, repeated, candidates,
            )
    result = finalize_evaluation(
        ranking, health, experiment_id, audit=champion_audit,
        standard_version=protocol.standard_version,
    )
    metric_rows = [
        item.to_dict()
        for collection in (screening, formal, stress, repeated)
        for item in collection
    ]
    result_document = {
        **result.to_dict(),
        "standard_version": protocol.standard_version,
        "input_hash": input_hash,
        "canonical_metric_hash": _canonical_hash(metric_rows),
    }
    if reuse:
        result_document["artifact_reuse_diagnostics"] = reuse_diagnostics
    if machine_report is not None:
        result_document["machine_evaluation"] = {
            "report_id": machine_report.report_id,
            "candidate_id": machine_report.candidate_id,
            "candidate_hash": machine_report.candidate_hash,
            "machine_verdict": machine_report.machine_verdict.value,
            "risk_label": None
            if machine_report.risk_label is None
            else machine_report.risk_label.value,
            "evidence_hash": machine_report.evidence_hash,
            "report_hash": machine_report.report_hash,
            "checks": [item.to_dict() for item in machine_report.checks],
        }

    comparisons = []
    by_key = {(x.candidate_id, x.window_id): x for x in formal}
    margins = resolve_margins(protocol)
    for candidate_id in shortlist.candidate_ids:
        for window in protocol.decision_windows:
            left, right = by_key.get((candidate_id, window)), by_key.get((protocol.incumbent_id, window))
            if left and right:
                values = _applicable_comparisons(protocol, left, right, margins)
                comparisons.extend({"candidate_id": candidate_id, **item.to_dict()} for item in values)
    documents = {
        "screening_metrics.csv": _csv_text([item.to_dict() for item in screening]),
        "screening_noninferiority.csv": _csv_text(screening_comparisons),
        "screening_decisions.csv": _csv_text(screening_decisions),
        "formal_metrics.csv": _csv_text([item.to_dict() for item in formal]),
        "noninferiority.csv": _csv_text(comparisons),
        "pareto_profiles.csv": _csv_text([item.to_dict() for item in ranking.profiles]),
        "health_check.json": json.dumps(health.to_dict() if health else None, ensure_ascii=False, indent=2) + "\n",
        "trial_ledger.csv": _csv_text([item.to_dict() for item in trials]),
    }
    if champion_audit is not None:
        documents.update({
            "statistical_audit.json": json.dumps(
                champion_audit.to_dict(), ensure_ascii=False, indent=2,
            ) + "\n",
            "cscv_splits.csv": _csv_text([
                item.to_dict() for item in champion_audit.search_bias.splits
            ]) if champion_audit.search_bias is not None else "",
            "bootstrap_comparisons.csv": _csv_text([
                item.to_dict() for item in champion_audit.bootstrap
            ]),
            "parameter_neighborhood.csv": _csv_text([
                item.to_dict() for item in champion_audit.neighborhood.neighbors
            ]) if champion_audit.neighborhood is not None else "",
            "execution_stress.csv": _csv_text([
                item.to_dict() for item in champion_audit.stress.comparisons
            ]) if champion_audit.stress is not None else "",
            "candidate_returns.csv": _return_matrix_csv(audit_request.search_returns),
            "comparison_returns.csv": _return_matrix_csv(audit_request.comparison_returns),
        })
    if machine_report is not None:
        documents["machine_evaluation.json"] = json.dumps(
            machine_report.to_dict(), ensure_ascii=False, indent=2,
        ) + "\n"
    if reuse:
        documents["artifact_reuse.csv"] = _csv_text([asdict(item) for item in reuse_ledger])
    result_document["audit_artifacts"] = {
        name: _text_hash(documents[name])
        for name in SCREENING_AUDIT_FILES
    }
    if reuse:
        result_document["audit_artifacts"]["artifact_reuse.csv"] = _text_hash(
            documents["artifact_reuse.csv"]
        )
    if champion_audit is not None:
        for name in (
            "statistical_audit.json", "cscv_splits.csv", "bootstrap_comparisons.csv",
            "parameter_neighborhood.csv", "execution_stress.csv", "candidate_returns.csv",
            "comparison_returns.csv",
        ):
            result_document["audit_artifacts"][name] = _text_hash(documents[name])
    if machine_report is not None:
        result_document["audit_artifacts"]["machine_evaluation.json"] = _text_hash(
            documents["machine_evaluation.json"]
        )
    result_document["decision_hash"] = _canonical_hash(result_document)
    documents = {
        "evaluation_result.json": json.dumps(result_document, ensure_ascii=False, indent=2) + "\n",
        "evaluation_report.md": render_summary(result),
        **documents,
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temporary = create_temporary_directory(
        experiment,
        "evaluation",
        prefix=f"{experiment.name.lower()}-",
        repository_root=context.root,
    )
    try:
        for name, text in documents.items():
            (temporary / name).write_text(text, encoding="utf-8", newline="\n")
        commit_order = [name for name in documents if name != "evaluation_result.json"]
        commit_order.append("evaluation_result.json")
        for name in commit_order:
            (temporary / name).replace(artifact_dir / name)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return CommandResult("PASS", "strategy.evaluate", result_document, {"directory": str(artifact_dir)})
