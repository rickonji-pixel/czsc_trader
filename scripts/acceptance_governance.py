"""Isolated, real-computation acceptance of the three human governance gates.

Run with the project interpreter. Generates deterministic synthetic market inputs.
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


def prepare(root, bundle):
    """Synthetic fixture only: real files, real loaders, no monkeypatches."""
    import numpy as np
    import pandas as pd
    from strategy_manager import canonical_sha256
    from czsc_trader.data import SESSION_TIMES

    (root / "src" / "czsc_trader").mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
    pool = root / "data" / "raw"
    pool.mkdir(parents=True)
    write(root / "strategies" / "registry.json", {"schema_version": 1, "strategies": []})
    package = bundle / "runtime" / "strategy_runtime"
    shutil.copytree(ROOT / "packages/strategy_runtime/src/strategy_runtime", package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "tests/fixtures/candidate_runtime.py", package / "strategies/candidate_fixture.py")
    sys.path.insert(0, str(bundle / "runtime"))
    if "strategy_runtime" in sys.modules:
        raise RuntimeError("Acceptance must start in a fresh interpreter")
    from strategy_runtime import StrategyCandidate, StrategyLoader
    from strategy_runtime.implementation_identity import implementation_sha256
    from czsc_trader.application.runtime_acceptance import _runtime_report

    payload = {"runtime": {
        "module": "strategy_runtime.strategies.candidate_fixture", "qualname": "CandidateFixture",
        "contract_version": 1, "source_files": ["strategies/candidate_fixture.py"],
        "source_sha256": implementation_sha256(("strategies/candidate_fixture.py",)),
    }}
    sessions = pd.bdate_range("2026-01-05", periods=132)
    changes = np.array([.02 if i % 2 else -.02 for i in range(len(sessions))])
    changes[::14], changes[13::14] = .015, -.015
    closes = 2 * np.cumprod(1 + changes)
    daily = pd.DataFrame({"date": sessions, "open": np.r_[2.0, closes[:-1]], "close": closes,
                          "volume": 80000.0, "amount": 160000.0})
    daily["high"] = daily[["open", "close"]].max(axis=1)
    daily["low"] = daily[["open", "close"]].min(axis=1)
    intraday = []
    for row in daily.itertuples():
        for clock in SESSION_TIMES:
            intraday.append({"datetime": pd.Timestamp(f"{row.date.date()} {clock}"),
                             "open": row.open, "high": row.high, "low": row.low, "close": row.close,
                             "volume": row.volume / 8, "amount": row.amount / 8})
    weekly = daily.assign(week=daily.date.dt.to_period("W-SUN")).groupby("week").agg(
        date=("date", "max"), open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), amount=("amount", "sum"),
    ).reset_index(drop=True)
    records = {}
    for freq, frame in (("daily", daily), ("30m", pd.DataFrame(intraday)), ("weekly", weekly)):
        name = f"588080_{freq}_2026.csv"
        frame.to_csv(pool / name, index=False)
        dates = frame["datetime" if freq == "30m" else "date"]
        records[name] = {"frequency": freq, "rows": len(frame), "first": dates.min().isoformat(),
                         "last": dates.max().isoformat(), "sha256": digest(pool / name)}
    base = {"symbol": "588080.SH", "name": "Synthetic acceptance fixture", "asset_type": "etf",
            "requested_start": sessions[0].date().isoformat(), "requested_end": sessions[-1].date().isoformat()}
    write(pool / "588080_manifest.json", {**base, "files": records})
    write(pool / "588080_validation.json", {"status": "PASS", "scope": "synthetic fixture; loaders independently reconcile"})
    shutil.copy2(pool / "588080_daily_2026.csv", pool / "588080_execution_daily_2026.csv")
    write(pool / "588080_execution_manifest.json", {
        **base, "adjustment": "none", "next_trading_session": (sessions[-1] + pd.offsets.BDay()).date().isoformat(),
        "files": {"588080_execution_daily_2026.csv": records["588080_daily_2026.csv"]},
    })
    flow_values = np.array([.51 + .02 * ((i // 2) % 10) if i % 2 == 0
                            else .49 - .02 * ((i // 2) % 10) for i in range(len(sessions))])
    sources = []
    for name, dataset, symbol, frame in (
        ("flow.csv", "etf.share", "588080.SH", pd.DataFrame({"Date": sessions, "Flow": flow_values})),
        ("calendar.csv", "calendar.trading_sessions", "SSE",
         pd.DataFrame({"Date": pd.date_range(sessions[0], sessions[-1] + pd.Timedelta(days=20))}).assign(
             IsOpen=lambda frame: (frame.Date.dt.dayofweek < 5).astype(int))),
    ):
        frame.to_csv(pool / name, index=False)
        sources.append({"dataset": dataset, "symbol": symbol, "path": name, "sha256": digest(pool / name)})
    candidates = []
    points = [("C000", .5), ("C001", .5)] + [
        (f"C{i:03d}", threshold) for i, threshold in enumerate((.40, .42, .44, .46, .48, .52, .54, .56, .58, .60), start=2)]
    for identity, threshold in points:
        params = copy.deepcopy(payload)
        params["parameters"] = {"threshold": threshold, "with_calendar": True, "entry_premium": .01}
        if identity == "C000":
            params["parameters"]["invert"] = True
        strategy = StrategyLoader().load_candidate(StrategyCandidate("S900", identity, params))
        candidates.append({"candidate_id": identity, "strategy_id": "S900", "strategy_payload": params,
                           "strategy_hash": canonical_sha256(params),
                           "execution_policy_hash": _runtime_report(strategy)["execution_policy_sha256"],
                           "parameter_distance": abs(threshold - .5),
                           "behavior_hash": canonical_sha256(
                               ((flow_values <= threshold) if identity == "C000" else (flow_values > threshold)).tolist()),
                           "is_incumbent": identity == "C000"})
    manifest = {
        "schema_version": 1, "experiment_id": EXPERIMENT, "strategy_id": "S900",
        "symbol": "588080.SH", "asset_type": "etf", "fee_rate": .001, "init_cash": 100000.0,
        "forward_start": (sessions[-1] + pd.offsets.BDay()).date().isoformat(),
        "windows": {"full": {"start": sessions[1].date().isoformat(), "end": sessions[-1].date().isoformat()}},
        "candidates": candidates, "evaluation_workers": 1, "review_data_sources": sources,
        "trials": [{"trial_id": item["candidate_id"], "candidate_id": item["candidate_id"],
                    "strategy_hash": item["strategy_hash"], "behavior_hash": item["behavior_hash"],
                    "status": "COMPLETED"} for item in candidates],
    }
    protocol = {
        "schema_version": 1, "standard_version": "opc-v3", "experiment_id": EXPERIMENT,
        "research_objective": "Isolated software acceptance only",
        "development_cutoff": sessions[-1].date().isoformat(),
        "incumbent_id": "C000", "incumbent_hash": candidates[0]["strategy_hash"],
        "decision_windows": ["full"], "target_windows": ["full"],
        "execution_policy_hash": candidates[0]["execution_policy_hash"], "tightened_margins": {},
        "shortlist_limit": 5, "candidate_manifest": "candidate_manifest.json",
        "target_requirements": [{"metric": "full_return", "direction": "maximize", "minimum_improvement": 0.0}],
    }
    write(experiment(root) / "candidate_manifest.json", manifest)
    write(experiment(root) / "evaluation_protocol.json", protocol)


def prepare_submission(root, bundle):
    from strategy_manager import canonical_sha256
    from strategy_runtime import StrategyCandidate, StrategyLoader
    from strategy_evaluator import EvaluationProtocol
    from czsc_trader.application.context import RepositoryContext
    from czsc_trader.application.evaluation_service import evaluate_experiment
    from czsc_trader.application.review_data import publish_review_dataset
    from czsc_trader.application.runtime_acceptance import _runtime_report

    announce("Preparing truthful claims using real TDR/SE/TXE computation...")
    context = RepositoryContext.discover(root)
    manifest = read(experiment(root) / "candidate_manifest.json")
    protocol = EvaluationProtocol.from_dict(read(experiment(root) / "evaluation_protocol.json"))
    directory = root / "data/review/preparation"
    published = publish_review_dataset(context, manifest, protocol, directory)
    result = evaluate_experiment(context, EXPERIMENT, use_cached_result=False, allow_artifact_reuse=False,
                                 review_data_root=directory, review_data_hash=published["snapshot_hash"])
    write(bundle / "preparation_result.json", result.result)
    candidate_id = "C001"  # Predeclared fixture, never select by acceptance output.
    candidate = next(item for item in manifest["candidates"] if item["candidate_id"] == candidate_id)
    with (experiment(root) / "artifacts/formal_metrics.csv").open(encoding="utf-8", newline="") as stream:
        metric = next(item for item in csv.DictReader(stream) if item["candidate_id"] == candidate_id)
    payload = candidate["strategy_payload"]
    strategy = StrategyLoader().load_candidate(StrategyCandidate("S900", candidate_id, payload))
    ready = _runtime_report(strategy)
    snapshot = {"schema_version": 1, "strategy_id": "S900", "candidate_id": candidate_id,
                "source_experiment": "experiments/S900/ACCEPTANCE", "strategy_payload": payload,
                "data_contract": {"symbol": "588080.SH", "asset_type": "etf",
                                  "requirements": ready["input_contract"]["requirements"]},
                "execution_policy": ready["execution_policy"],
                "research_claims": {"annual_return": {"value": float(metric["net_cagr"]), "tolerance": 1e-10}}}
    snapshot["candidate_hash"] = canonical_sha256(snapshot)
    mandate = {
        "schema_version": 1, "mandate_id": "EM-S900-DEMO", "strategy_id": "S900", "candidate_id": candidate_id,
        "development_cutoff": protocol.development_cutoff, "forward_start": manifest["forward_start"],
        "evaluation_windows": manifest["windows"], "benchmark": {"type": "strategy", "id": "C000"},
        "objectives": [{"metric": "annual_return", "operator": ">=", "value": 0.0}],
        "cost_policy": {"primary_fee_rate": 0.001,
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
        "evidence_seen_through": protocol.development_cutoff, "finalized_at": datetime.now().astimezone().isoformat(),
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
    from strategy_manager import StrategyRegistry, canonical_sha256
    from strategy_runtime import StrategyCandidate, StrategyLoader, StrategyRelease
    from czsc_trader.application.runtime_acceptance import _runtime_report
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
        evaluation = cli(root, bundle, "03_evaluate", ["strategy", "review", "evaluate",
                         "--strategy", "S900", "--credential", CREDENTIAL])
        evaluated = require_pass(evaluation)
        report = evaluated["adjudication_report"]
        write(root / "adjudication.json", report)
        freeze_args = ["strategy", "freeze", "--strategy", "S900", "--credential", CREDENTIAL,
                       "--change-summary", "isolated acceptance only", *human]
        if name == "normal":
            # Copy the evaluated, not-yet-approved state to test post-review drift.
            drift = bundle / "evidence_drift"
            shutil.copytree(root, drift)
            metric = drift / evaluation["response"]["artifacts"]["source_experiment"] / "artifacts/formal_metrics.csv"
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
            registry = StrategyRegistry(root / "strategies")
            credential = registry.get_governance_credential("S900", CREDENTIAL)
            snapshot = read(root / "candidate.json")
            loader = StrategyLoader()
            candidate_runtime = _runtime_report(loader.load_candidate(
                StrategyCandidate("S900", "C001", snapshot["strategy_payload"])))
            frozen_runtime = _runtime_report(loader.load(StrategyRelease.from_mapping(
                registry.get_version("S900", "v1").to_dict())))
            runtime_same = all(candidate_runtime[field] == frozen_runtime[field] for field in (
                "input_contract", "execution_policy", "implementation", "parameters_sha256",
                "decision_contract", "state_mode", "capabilities_sha256", "monitoring_sha256"))
            required = set(read(root / "mandate.json")["required_audits"])
            complete = (set(report["audit_results"]) == required
                        and all(item["status"] == "PASS" for item in report["audit_results"].values()))
            sealed = [seal.stage.value for seal in credential.seals] == [
                "RESEARCH_INITIATED", "CANDIDATE_SUBMITTED", "TDR_ADJUDICATED", "FREEZE_APPROVED", "VERSION_FROZEN"]
            ok = (report["machine_verdict"] == "ELIGIBLE_FOR_FREEZE_REVIEW" and len(versions) == 1
                  and repeated.get("idempotent_replay") is True and before == after
                  and runtime_same and complete and sealed)
        else:
            reason = "CLAIM_MISMATCH" if name == "false_claim" else "AUDIT_MISSING:monitoring_plan"
            ok = report["blocking_findings"] == [reason] and frozen["exit_code"] != 0 and not versions
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
        prepare(bundle / "prepared", bundle)
        prepare_submission(bundle / "prepared", bundle)
        results = run_cases(bundle)
        unchanged = all(digest(ROOT / name) == value for name, value in protected.items())
        passed = all(item["passed"] for item in results) and unchanged
        write(bundle / "acceptance.json", {"status": "PASS" if passed else "FAIL",
              "source_strategies_unchanged": unchanged, "cases": results,
              "scope": "Isolated CLI acceptance; synthetic data, no mocks, no production deployment or alpha validation"})
        if not passed:
            raise RuntimeError("Acceptance assertions failed; see acceptance.json")
    except Exception as exc:
        write(bundle / "failure.json", {"error": str(exc), "type": type(exc).__name__})
        raise
    announce(f"Acceptance complete: {bundle / 'acceptance.json'}")


if __name__ == "__main__":
    main()
