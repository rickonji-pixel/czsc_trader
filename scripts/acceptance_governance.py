"""Isolated, real-computation acceptance of the three human governance gates.

Run with the project interpreter. Local archived S001 inputs are required.
All generated registries, runtime bindings and evidence remain under .tmp.
This is a software acceptance fixture, not a new strategy research result.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = "ACCEPTANCE"
CREDENTIAL = "SGC-S900-001"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def announce(message):
    print(message, flush=True)


def experiment(root):
    return root / "experiments" / "S900" / EXPERIMENT


def prepare(root):
    from strategy_manager import canonical_sha256

    (root / "src" / "czsc_trader").mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
    (root / "data" / "raw").mkdir(parents=True)
    for path in sorted((ROOT / "data" / "raw").glob("588080*")):
        if path.is_file():
            shutil.copy2(path, root / "data" / "raw" / path.name)
    shutil.copytree(ROOT / "strategies" / "dependencies", root / "strategies" / "dependencies")
    for reference in (
        "S001/0824_EX04/artifacts/frozen_challenger.json",
    ):
        destination = root / "experiments" / reference
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "experiments" / reference, destination)
    write(root / "strategies" / "registry.json", {"schema_version": 1, "strategies": []})
    source = read(ROOT / "experiments/S001/0903_EX04/candidate_manifest.json")
    center = next(item for item in source["candidates"] if item["candidate_id"] == "R1102")
    weights = center["strategy_payload"]["rule"]["weights"]["range"]

    def distance(item):
        other = item["strategy_payload"]["rule"]["weights"]["range"]
        return sum(abs(weights[name] - other[name]) for name in weights)

    # Selection is by parameter distance only, never by acceptance output.
    selected = sorted(source["candidates"][1:], key=lambda item: (distance(item), item["candidate_id"]))[:24]
    candidates = copy.deepcopy([source["candidates"][0], *selected])
    execution = {"policy_type": "FROZEN_RULE", "settings": center["strategy_payload"]["rule"]["execution"]}
    execution_hash = canonical_sha256(execution)
    for item in candidates:
        item["execution_policy_hash"] = execution_hash
        item["strategy_hash"] = canonical_sha256(item["strategy_payload"])
    manifest = {
        "schema_version": 1, "experiment_id": EXPERIMENT,
        "symbol": "588080.SH", "asset_type": "etf", "fee_rate": 0.0005,
        "init_cash": 1000000.0, "forward_start": "2026-09-03",
        "windows": {"full": {"start": "2021-01-04", "end": "2026-09-02"}},
        "candidates": candidates, "evaluation_workers": 1,
        "audit_protocol": {"seed": 20260919, "bootstrap_repetitions": 200, "mean_block_lengths": [21, 10, 42]},
        "trials": [{"trial_id": f"DEMO-{i:03d}", "candidate_id": item["candidate_id"],
                    "strategy_hash": item["strategy_hash"], "behavior_hash": item["behavior_hash"],
                    "status": "COMPLETED"} for i, item in enumerate(candidates)],
    }
    protocol = read(ROOT / "experiments/S001/0903_EX04/evaluation_protocol.json")
    protocol.update(standard_version="opc-v3", experiment_id=EXPERIMENT,
                    research_objective="Isolated software acceptance only",
                    incumbent_hash=candidates[0]["strategy_hash"],
                    decision_windows=["full"], target_windows=["full"],
                    execution_policy_hash=execution_hash, shortlist_limit=24,
                    target_requirements=[{"metric": "full_return", "direction": "maximize", "minimum_improvement": 0.0}])
    write(experiment(root) / "candidate_manifest.json", manifest)
    write(experiment(root) / "evaluation_protocol.json", protocol)


def prepare_submission(root, bundle):
    from strategy_manager import canonical_sha256
    from czsc_trader.application.context import RepositoryContext
    from czsc_trader.application.evaluation_service import evaluate_experiment

    announce("Preparing truthful claims using real TDR/SE/TXE computation...")
    result = evaluate_experiment(RepositoryContext.discover(root), EXPERIMENT,
                                 use_cached_result=False, allow_artifact_reuse=False)
    write(bundle / "preparation_result.json", result.result)
    candidate_id = result.result.get("recommended_candidate_id")
    if not candidate_id:
        raise RuntimeError("Real fixture evaluation produced no candidate; see preparation_result.json")
    manifest = read(experiment(root) / "candidate_manifest.json")
    candidate = next(item for item in manifest["candidates"] if item["candidate_id"] == candidate_id)
    with (experiment(root) / "artifacts/formal_metrics.csv").open(encoding="utf-8", newline="") as stream:
        metric = next(item for item in csv.DictReader(stream) if item["candidate_id"] == candidate_id)
    runtime_root = bundle / "runtime"
    package = runtime_root / "strategy_runtime"
    shutil.copytree(ROOT / "packages/strategy_runtime/src/strategy_runtime", package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    wrapper = package / "strategies/s900_v1.py"
    wrapper.write_text('from .s001_common import S001Base\n\nclass S900V1(S001Base):\n    expected_release_id = "S900-v1"\n', encoding="utf-8")
    sys.path.insert(0, str(runtime_root))
    # SRT may already be imported by evaluation; use the copied package for the
    # fixture wrapper and resource binding without changing any existing code.
    for name in tuple(sys.modules):
        if name == "strategy_runtime" or name.startswith("strategy_runtime."):
            del sys.modules[name]
    from strategy_runtime import StrategyRelease
    from strategy_runtime.strategies.s900_v1 import S900V1
    from strategy_runtime.implementation_identity import implementation_sha256

    payload = candidate["strategy_payload"]
    release_fields = {"schema_version": 3, "strategy_id": "S900", "version": "v1",
                      "release_id": "S900-v1", "strategy_payload": payload}
    release_hash = canonical_sha256(release_fields)
    release = StrategyRelease.from_mapping({**release_fields, "release_hash": release_hash})
    strategy = S900V1.from_release(release)
    sources = ("strategies/s900_v1.py", "strategies/s001_common.py", "execution_planner.py")
    write(package / "bindings/S900-v1.json", {"release_id": "S900-v1", "release_hash": release_hash,
          "source_files": list(sources), "implementation_sha256": implementation_sha256(sources)})
    from dataclasses import asdict
    execution = {"policy_type": "FROZEN_RULE", "settings": payload["rule"]["execution"]}
    snapshot = {"schema_version": 1, "strategy_id": "S900", "candidate_id": candidate_id,
                "source_experiment": "experiments/S900/ACCEPTANCE", "strategy_payload": payload,
                "data_contract": {"symbol": "588080.SH", "asset_type": "etf",
                                  "requirements": [asdict(item) for item in strategy.definition.inputs.requirements]},
                "execution_policy": execution,
                "research_claims": {"annual_return": {"value": float(metric["net_cagr"]), "tolerance": 1e-10}}}
    snapshot["candidate_hash"] = canonical_sha256(snapshot)
    mandate = {
        "schema_version": 1, "mandate_id": "EM-S900-DEMO", "strategy_id": "S900", "candidate_id": candidate_id,
        "development_cutoff": "2026-09-02", "forward_start": "2026-09-03",
        "evaluation_windows": manifest["windows"], "benchmark": {"type": "strategy", "id": "S001-v1"},
        "objectives": [{"metric": "annual_return", "operator": ">=", "value": 0.0}],
        "cost_policy": {"primary_fee_rate": 0.0005,
                        "stress_scenarios": ["total_cost_15bp", "total_cost_20bp", "total_cost_30bp", "total_cost_50bp"],
                        "blocking_scenarios": ["total_cost_15bp"], "minimum_cagr": 0.0,
                        "max_drawdown_floor": -1.0, "minimum_calmar": 0.0},
        "frequency_policy": {"mode": "OBSERVE", "window_days": 60},
        "audit_requirements": {
            "parameter_robustness": {"minimum_valid_neighbors": 10},
            "statistical_robustness": {"allowed_risk_labels": ["FAVORABLE", "MIXED"],
                                     "minimum_bootstrap_probability": 0.5, "maximum_pbo": 0.5, "minimum_dsr_probability": 0.5},
            "technical_replay": {"mode": "FULL_RECOMPUTE", "allow_artifact_reuse": False, "execution_engine": "TXE-v1"},
            "external_validation": {"required_replays": 0, "minimum_cagr": 0.0, "max_drawdown_floor": -1.0},
            "runtime_acceptance": {"required_status": "PASS"},
            "monitoring_plan": {"required_status": "APPROVED", "minimum_rules": 1}},
        "required_audits": ["objective_recalculation", "frequency_recalculation", "parameter_robustness",
                            "statistical_robustness", "cost_stress", "technical_replay", "external_validation",
                            "runtime_acceptance", "monitoring_plan"],
        "evidence_seen_through": "2026-09-02", "finalized_at": datetime.now().astimezone().isoformat(),
        "finalized_by": "acceptance-demo",
    }
    mandate["mandate_hash"] = canonical_sha256(mandate)
    write(root / "candidate.json", snapshot)
    write(root / "mandate.json", mandate)
    write(root / "research_request.json", {"strategy_id": "S900", "name": "隔离验收样例",
          "scope": {"symbols": ["588080.SH"]}, "research_intent": {"purpose": "软件验收，不构成策略推荐"}})
    write(experiment(root) / "artifacts/external_validation.json",
          {"candidate_id": candidate_id, "candidate_hash": snapshot["candidate_hash"], "replays": []})
    write(experiment(root) / "artifacts/monitoring_plan.json", {"status": "APPROVED", "approved_by": "acceptance-demo",
          "rules": [{"metric": "execution_error_count", "operator": ">", "threshold": 0, "action": "REVIEW"}]})


def cli(root, bundle, label, arguments):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(bundle / "runtime") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    command = [sys.executable, "-B", "-c", "from czsc_trader.cli.main import main; raise SystemExit(main())",
               *arguments, "--repo-root", str(root)]
    announce(f"{root.name}: {label}")
    started = time.monotonic()
    completed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / f"{label}.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (root / "logs" / f"{label}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    documents = []
    for line in completed.stdout.splitlines():
        try:
            documents.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    response = next((item for item in reversed(documents) if isinstance(item, dict) and "status" in item), None)
    record = {"arguments": arguments, "exit_code": completed.returncode,
              "seconds": round(time.monotonic() - started, 3), "response": response}
    write(root / "logs" / f"{label}.json", record)
    if response is None:
        raise RuntimeError(f"{label}: missing CLI JSON; inspect logs")
    return record


def require_pass(record):
    if record["exit_code"] != 0 or record["response"]["status"] != "PASS":
        raise RuntimeError(json.dumps(record, ensure_ascii=False))
    return record["response"].get("result", {})


def run_cases(bundle):
    from strategy_manager import canonical_sha256
    prepared = bundle / "prepared"
    summary = []
    for name in ("normal", "false_claim", "missing_evidence"):
        root = bundle / name
        shutil.copytree(prepared, root)
        if name == "false_claim":
            snapshot = read(root / "candidate.json")
            snapshot["research_claims"]["annual_return"]["value"] += 1.0
            snapshot.pop("candidate_hash")
            snapshot["candidate_hash"] = canonical_sha256(snapshot)
            write(root / "candidate.json", snapshot)
            external = read(experiment(root) / "artifacts/external_validation.json")
            external["candidate_hash"] = snapshot["candidate_hash"]
            write(experiment(root) / "artifacts/external_validation.json", external)
        if name == "missing_evidence":
            # Exact fixture file, under this newly created isolated case only.
            (experiment(root) / "artifacts/monitoring_plan.json").unlink()
        human = ["--actor", "acceptance-demo", "--reason", "isolated software acceptance"]
        require_pass(cli(root, bundle, "01_create", ["research", "create", "--input", "research_request.json", *human]))
        require_pass(cli(root, bundle, "02_open", ["strategy", "review", "open", "--credential", CREDENTIAL,
                    "--candidate", "candidate.json", "--mandate", "mandate.json", *human]))
        evaluated = require_pass(cli(root, bundle, "03_evaluate", ["strategy", "review", "evaluate",
                                 "--strategy", "S900", "--credential", CREDENTIAL]))
        report = evaluated["adjudication_report"]
        write(root / "adjudication.json", report)
        freeze_args = ["strategy", "freeze", "--strategy", "S900", "--credential", CREDENTIAL,
                       "--change-summary", "isolated acceptance only", *human]
        if name == "normal":
            # Copy the evaluated, not-yet-approved state to test post-review drift.
            drift = bundle / "evidence_drift"
            shutil.copytree(root, drift)
            metric = experiment(drift) / "artifacts/formal_metrics.csv"
            metric.write_bytes(metric.read_bytes() + b"\n")
            rejected = cli(drift, bundle, "04_freeze", freeze_args)
            drift_ok = (rejected["exit_code"] != 0
                        and "adjudication evidence changed after review" in rejected["response"].get("error", {}).get("message", "")
                        and not list((drift / "strategies/S900/versions").glob("*.json")))
            summary.append({"case": "evidence_drift", "passed": drift_ok, "response": rejected["response"]})
        frozen = cli(root, bundle, "04_freeze", freeze_args)
        versions = list((root / "strategies/S900/versions").glob("*.json"))
        if name == "normal":
            require_pass(frozen)
            before = {str(path.relative_to(root)): digest(path) for path in (root / "strategies").rglob("*") if path.is_file()}
            repeated = require_pass(cli(root, bundle, "05_repeat_freeze", freeze_args))
            after = {str(path.relative_to(root)): digest(path) for path in (root / "strategies").rglob("*") if path.is_file()}
            ok = (report["machine_verdict"] == "ELIGIBLE_FOR_FREEZE_REVIEW" and len(versions) == 1
                  and repeated.get("idempotent_replay") is True and before == after)
        else:
            reason = "CLAIM_MISMATCH" if name == "false_claim" else "AUDIT_MISSING:monitoring_plan"
            ok = reason in report["blocking_findings"] and frozen["exit_code"] != 0 and not versions
        summary.append({"case": name, "passed": ok, "verdict": report["machine_verdict"],
                        "blocking_findings": report["blocking_findings"]})
        write(bundle / "summary.json", summary)
        if not ok:
            raise RuntimeError(f"Acceptance case failed: {name}; inspect logs")
    return summary


def main():
    bundle = ROOT / ".tmp" / f"governance-acceptance-{datetime.now():%Y%m%d-%H%M%S-%f}"
    if not bundle.is_relative_to((ROOT / ".tmp").resolve()):
        raise ValueError("Acceptance output must remain under repository .tmp")
    bundle.mkdir(parents=True, exist_ok=False)
    announce(f"Acceptance bundle: {bundle}")
    protected = {str(path.relative_to(ROOT)): digest(path) for path in (ROOT / "strategies").rglob("*.json")}
    write(bundle / "source_snapshot.json", protected)
    try:
        prepare(bundle / "prepared")
        prepare_submission(bundle / "prepared", bundle)
        results = run_cases(bundle)
        unchanged = all(digest(ROOT / name) == value for name, value in protected.items())
        passed = all(item["passed"] for item in results) and unchanged
        write(bundle / "acceptance.json", {"status": "PASS" if passed else "FAIL",
              "source_strategies_unchanged": unchanged, "cases": results,
              "scope": "Isolated CLI acceptance; historical data, no production deployment or alpha validation"})
        if not passed:
            raise RuntimeError("Acceptance assertions failed; see acceptance.json")
    except Exception as exc:
        write(bundle / "failure.json", {"error": str(exc), "type": type(exc).__name__})
        raise
    announce(f"Acceptance complete: {bundle / 'acceptance.json'}")


if __name__ == "__main__":
    main()
