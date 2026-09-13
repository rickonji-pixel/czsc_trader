from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX01"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    s001_experiment = repo / "experiments/S001/20260913_S001_EX01"
    s004_experiment = repo / "experiments/S004/20260913_S004_EX41"
    validate_experiment_archive(s001_experiment)
    validate_experiment_archive(s004_experiment)
    s001_path = s001_experiment / "artifacts/gap_summary.json"
    s004_path = s004_experiment / "artifacts/candidate_metrics.json"
    s001 = _read(s001_path)
    s004 = _read(s004_path)

    full_window = s001["full_window"]
    s001_cagr = float(full_window["strategy_cagr"])
    s001_max_drawdown = float(full_window["strategy_max_drawdown"])
    frequency_median = float(s004["rolling_60_median_observation"])
    frequency_p10 = float(s004["rolling_60_p10_observation"])
    minimum_cagr = s001_cagr * float(protocol["minimum_cagr_ratio_to_s001_v2"])

    if frequency_median != float(protocol["minimum_rolling_60_closed_trades_median"]):
        raise ValueError("S004 frequency median differs from frozen S005 premise")
    if frequency_p10 != float(protocol["minimum_rolling_60_closed_trades_p10"]):
        raise ValueError("S004 frequency P10 differs from frozen S005 premise")

    contract = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S005",
        "status": "MECHANISM_DISCOVERY",
        "symbol": protocol["symbol"],
        "development_window": {
            "start": protocol["development_start"],
            "end": protocol["development_cutoff"],
        },
        "benchmarks": {
            "risk_return": {
                "strategy": "S001-v2",
                "cagr": s001_cagr,
                "maximum_drawdown": s001_max_drawdown,
            },
            "frequency": {
                "candidate": "S004-C002",
                "closed_trades": int(s004["filtered_episodes"]),
                "rolling_60_median": frequency_median,
                "rolling_60_p10": frequency_p10,
            },
        },
        "minimum_candidate_gates": {
            "cagr": minimum_cagr,
            "maximum_drawdown": float(protocol["maximum_drawdown_limit"]),
            "rolling_60_independent_closed_trades_median": frequency_median,
            "rolling_60_independent_closed_trades_p10": frequency_p10,
        },
        "secondary_benchmark": "BuyHold",
        "comparison_rule": "ALL_MINIMUM_GATES_MUST_PASS_BEFORE_FORMAL_CANDIDATE_REGISTRATION",
        "anti_shortcuts": [
            "NO_POSITION_SCALING_AS_ALPHA",
            "NO_ORDER_SPLITTING_AS_INDEPENDENT_TRADES",
            "NO_UNRELATED_RULE_BUNDLING_FOR_FREQUENCY",
            "NO_FORWARD_DATA_TUNING",
        ],
        "lifecycle_effects": {
            "candidate_created": False,
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
        "evidence": {
            "s001": {"path": str(s001_path.relative_to(repo)).replace("\\", "/"), "sha256": raw_file_sha256(s001_path)},
            "s004": {"path": str(s004_path.relative_to(repo)).replace("\\", "/"), "sha256": raw_file_sha256(s004_path)},
            "protocol": {"path": str(protocol_path.relative_to(repo)).replace("\\", "/"), "sha256": raw_file_sha256(protocol_path)},
        },
    }
    _write(artifacts / "benchmark_contract.json", contract)
    (experiment / "03_execution.md").write_text(
        "# S005 EX01 执行\n\n状态：COMPLETE。已校验 S001 与 S004 权威档案，并固化 S005 双标杆合同。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX01 结论\n\n"
        f"S005进入 `MECHANISM_DISCOVERY`。正式候选至少需要：完整开发池年化收益{minimum_cagr:.2%}，"
        f"最大回撤不深于{float(protocol['maximum_drawdown_limit']):.2%}，滚动60交易日独立闭合交易"
        f"中位数不少于{frequency_median:.0f}笔、P10不少于{frequency_p10:.0f}笔。"
        "本实验只冻结目标和标杆，没有生成候选、冻结策略或修改PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S005",
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "START_MECHANISM_DISCOVERY",
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()

