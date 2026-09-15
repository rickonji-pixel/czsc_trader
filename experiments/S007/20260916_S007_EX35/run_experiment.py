from __future__ import annotations

from dataclasses import replace
from datetime import date
import gzip
import importlib.util
import json
from pathlib import Path

import pandas as pd
from strategy_evaluator import (
    ChampionAuditRequest,
    ExternalReplayEvidence,
    MachineEvaluationCase,
    MachineEvaluationPolicy,
    RiskLabel,
    StressScenarioResult,
    evaluate_machine_eligibility,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260916_S007_EX35"
SOURCE_EXPERIMENT = "20260916_S007_EX34"
TOTAL_COST_BPS = (15, 20, 30, 50)


def _load_ex34_helpers(repo: Path):
    path = repo / "experiments/S007" / SOURCE_EXPERIMENT / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("s007_ex34_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load EX34 helpers: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_gzip_json(path: Path) -> dict[str, object]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _external_replays(rows: object) -> tuple[ExternalReplayEvidence, ...]:
    if not isinstance(rows, list):
        raise ValueError("external_replays must be a list")
    return tuple(
        ExternalReplayEvidence(
            str(item["replay_id"]),
            str(item["symbol"]),
            str(item["candidate_id"]),
            str(item["candidate_hash"]),
            tuple(map(str, item["dates"])),
            tuple(float(value) for value in item["returns"]),
        )
        for item in rows
    )


def main() -> None:
    repo = Path(__file__).resolve().parents[3]
    experiment = repo / "experiments/S007" / EXPERIMENT_ID
    artifacts = experiment / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    helpers = _load_ex34_helpers(repo)

    source_input = (
        repo / "experiments/S007" / SOURCE_EXPERIMENT
        / "artifacts/machine_evaluation_input.json.gz"
    )
    raw_case = _read_gzip_json(source_input)
    raw_request = raw_case.get("audit_request")
    if not isinstance(raw_request, dict):
        raise ValueError("EX34 machine input has no audit_request")
    request = ChampionAuditRequest.from_dict(raw_request)

    protocol = json.loads(
        (experiment / "artifacts/protocol.json").read_text(encoding="utf-8")
    )
    initial_cash = float(protocol["initial_cash"])
    candidate_account = pd.read_csv(
        repo / "experiments/S007/20260915_S007_EX31/artifacts/account_daily.csv"
    )
    buyhold_account = pd.read_csv(
        repo / "experiments/S007/20260915_S007_EX32/artifacts/buyhold_account_daily.csv"
    )
    fills = pd.read_csv(
        repo / "experiments/S007/20260915_S007_EX31/artifacts/fills.csv"
    )
    replay_data = load_replay_data(
        RepositoryContext.discover(repo),
        "research",
        str(protocol["symbol"]),
        "etf",
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    daily = replay_data.adjusted.daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"], errors="raise").dt.normalize()
    open_prices = daily.set_index("dt")["open"].astype(float)

    stress_results = []
    stress_rows = []
    for total_cost_bp in TOTAL_COST_BPS:
        scenario_id = f"total_cost_{total_cost_bp}bp"
        multiplier = total_cost_bp / 10.0
        candidate_returns, candidate_trades, candidate_total = helpers._adjusted_fill_replay(
            candidate_account,
            fills,
            initial_cash,
            fee_multiplier=multiplier,
            slippage_bp=0,
        )
        benchmark_returns, benchmark_total = helpers._buyhold_stress(
            buyhold_account,
            open_prices,
            initial_cash,
            base_fee=0.001,
            fee_multiplier=multiplier,
            slippage_bp=0,
        )
        candidate_observation = helpers._observation(
            request.champion_id,
            scenario_id,
            candidate_returns,
            candidate_total,
            candidate_trades,
            tier="stress_replay",
        )
        benchmark_observation = helpers._observation(
            request.incumbent_id,
            scenario_id,
            benchmark_returns,
            benchmark_total,
            None,
            tier="stress_replay",
        )
        stress_results.append(StressScenarioResult(
            scenario_id,
            request.identity.execution_policy_hash,
            (candidate_observation, benchmark_observation),
        ))
        stress_rows.append({
            "scenario_id": scenario_id,
            "blocking": total_cost_bp == 15,
            "net_cagr": candidate_observation.net_cagr,
            "max_drawdown": candidate_observation.max_drawdown,
            "calmar": candidate_observation.calmar,
            "profit_factor": candidate_observation.profit_factor,
            "closed_trades": candidate_observation.closed_trades,
        })

    request = replace(
        request,
        identity=replace(request.identity, experiment_id=EXPERIMENT_ID),
        stress_results=tuple(stress_results),
    )
    policy_data = protocol["policy"]
    policy = MachineEvaluationPolicy(
        policy_id=str(policy_data["policy_id"]),
        policy_version=str(policy_data["policy_version"]),
        allowed_risk_labels=(RiskLabel.FAVORABLE, RiskLabel.MIXED),
        minimum_bootstrap_probability=float(policy_data["minimum_bootstrap_probability"]),
        required_external_replays=int(policy_data["required_external_replays"]),
        external_minimum_cagr=float(policy_data["external_minimum_cagr"]),
        external_max_drawdown_floor=float(policy_data["external_max_drawdown_floor"]),
        stress_minimum_cagr=float(policy_data["stress_minimum_cagr"]),
        stress_max_drawdown_floor=float(policy_data["stress_max_drawdown_floor"]),
        stress_minimum_calmar=float(policy_data["stress_minimum_calmar"]),
        blocking_stress_scenarios=tuple(policy_data["blocking_stress_scenarios"]),
    )
    case = MachineEvaluationCase(
        EXPERIMENT_ID,
        str(raw_case["candidate_hash"]),
        policy,
        request,
        _external_replays(raw_case.get("external_replays", [])),
    )
    report = evaluate_machine_eligibility(case)

    pd.DataFrame(stress_rows).to_csv(
        artifacts / "stress_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    helpers._write(artifacts / "machine_evaluation_report.json", report.to_dict())
    helpers._write_gzip_json(artifacts / "machine_evaluation_input.json.gz", case.to_dict())
    checks = {item.check_id: item.status.value for item in report.checks}
    (experiment / "03_execution.md").write_text(
        "# S007 EX35 执行\n\n"
        f"状态：`COMPLETE`。成本场景按单边总成本 15/20/30/50bp 重算。"
        f"SE检查结果：`{json.dumps(checks, ensure_ascii=False, sort_keys=True)}`。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX35 结论\n\n"
        f"SE机器裁决：`{report.machine_verdict.value}`；统计综合标签："
        f"`{None if report.risk_label is None else report.risk_label.value}`；"
        f"原因码：`{json.dumps(report.reason_codes, ensure_ascii=False)}`。\n\n"
        "20/30/50bp仅为诊断场景，未用于回答成本崩溃边界。本轮没有创建策略版本、"
        "冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "candidate_id": protocol["candidate_id"],
            "candidate_hash": protocol["candidate_hash"],
            "machine_verdict": report.machine_verdict.value,
            "risk_label": None if report.risk_label is None else report.risk_label.value,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()

