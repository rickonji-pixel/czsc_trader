from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX07"


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
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX01")
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX06")

    contract_path = repo / str(protocol["source_contract"])
    cost_path = repo / str(protocol["source_cost"])
    contract = _read(contract_path)
    cost = _read(cost_path)
    target_cagr = float(contract["minimum_candidate_gates"]["cagr"])
    one_way_cost = float(cost["execution"]["stress_one_way_cost"])
    sessions_per_year = int(protocol["sessions_per_year"])
    rows: list[dict[str, float]] = []
    for frequency in map(float, protocol["frequency_scenarios_per_60_sessions"]):
        annual_trades = frequency / 60.0 * sessions_per_year
        required_net = (1.0 + target_cagr) ** (1.0 / annual_trades) - 1.0
        required_gross = (1.0 + required_net) * (1.0 + one_way_cost) / (1.0 - one_way_cost) - 1.0
        rows.append(
            {
                "trades_per_60_sessions": frequency,
                "implied_annual_trades": annual_trades,
                "required_geometric_net_return_per_trade": required_net,
                "required_geometric_gross_return_per_trade": required_gross,
            }
        )
    frame = pd.DataFrame(rows)
    benchmark = frame.loc[frame["trades_per_60_sessions"].eq(7.0)].iloc[0]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "target_cagr": target_cagr,
        "stress_one_way_cost": one_way_cost,
        "frequency_benchmark_per_60": 7.0,
        "required_net_per_trade_at_benchmark": float(benchmark["required_geometric_net_return_per_trade"]),
        "required_gross_per_trade_at_benchmark": float(benchmark["required_geometric_gross_return_per_trade"]),
        "interpretation": "TINY_INTRADAY_EDGES_ARE_ECONOMICALLY_INSUFFICIENT; PRIORITIZE_MULTI_SESSION_SWING_MECHANISMS",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "sources": {
            "contract_sha256": raw_file_sha256(contract_path),
            "cost_sha256": raw_file_sha256(cost_path),
            "protocol_sha256": raw_file_sha256(protocol_path),
        },
    }
    frame.to_csv(artifacts / "feasibility_scenarios.csv", index=False, lineterminator="\n")
    _write(artifacts / "feasibility_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S005 EX07 执行\n\n状态：COMPLETE。已按固定目标、频率和压力成本完成单笔收益预算换算。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX07 结论\n\n"
        f"在滚动60日7笔、全年约{benchmark['implied_annual_trades']:.1f}笔的频率下，要实现"
        f"{target_cagr:.2%}年化，平均每笔至少需要{benchmark['required_geometric_net_return_per_trade']:.3%}"
        f"费后几何收益；计入单边{one_way_cost:.2%}压力成本后，毛收益至少约"
        f"{benchmark['required_geometric_gross_return_per_trade']:.3%}。\n\n"
        "此前日内小效应的量级不足以满足目标。后续停止搜索十几个基点的单日噪声，转向能提供"
        "多日价格空间的中频波段机制。本轮不生成候选，不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": "2026-09-02",
            "decision": "PRIORITIZE_MULTI_SESSION_SWING_MECHANISMS",
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()

