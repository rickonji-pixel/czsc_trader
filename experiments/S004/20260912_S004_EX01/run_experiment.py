from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.identity import canonical_json_sha256
from dataflows.bar_utils import (
    validate_a_share_intraday_bars,
    validate_intraday_against_daily,
)
from dataflows.tushare_etf import fetch_etf_ohlcv


EXPERIMENT_ID = "20260912_S004_EX01"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "candidate_generation",
        "parameter_search",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(bool(protocol.get(key)) for key in forbidden):
        raise ValueError("data feasibility experiment may not mutate lifecycle state")

    target = protocol["research_target"]
    start = str(protocol["probe_start"])
    end = str(protocol["probe_end"])
    daily, daily_metadata = fetch_etf_ohlcv(
        str(target["symbol"]), start, end, "daily", env_file=repo / ".env"
    )
    frequency_results: dict[str, object] = {}
    factor_hashes = {str(daily_metadata["adjustment_factor_sha256"])}
    expected = protocol["expected_bars_per_complete_day"]
    for period in ("15m", "5m", "1m"):
        frame, metadata = fetch_etf_ohlcv(
            str(target["symbol"]), start, end, period, env_file=repo / ".env"
        )
        session = validate_a_share_intraday_bars(
            frame, period, require_complete_days=True
        )
        reconciliation = validate_intraday_against_daily(frame, daily, period)
        if int(session["expected_bars_per_day"]) != int(expected[period]):
            raise ValueError(f"{period}: expected bar count differs from protocol")
        factor_hashes.add(str(metadata["adjustment_factor_sha256"]))
        timestamps = pd.to_datetime(frame["Date"], errors="raise")
        frequency_results[period] = {
            "status": "PASS",
            "bar_count": int(len(frame)),
            "complete_day_count": int(session["complete_day_count"]),
            "expected_bars_per_day": int(session["expected_bars_per_day"]),
            "first_bar": timestamps.min().isoformat(),
            "last_bar": timestamps.max().isoformat(),
            "daily_matched_days": int(reconciliation["matched_day_count"]),
            "metadata": metadata,
        }
    if len(factor_hashes) != 1:
        raise ValueError("minute and daily frequencies use different adjustment factors")

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "protocol_sha256": canonical_json_sha256(protocol),
        "daily_rows": int(len(daily)),
        "shared_adjustment_factor_sha256": factor_hashes.pop(),
        "frequencies": frequency_results,
        "scope": {
            "signal_census": False,
            "candidate_created": False,
            "strategy_manager_mutated": False,
            "pte_mutated": False,
        },
    }
    _write(artifacts / "data_feasibility.json", evidence)
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        "状态：COMPLETE。固定两交易日的15m、5m和1m数据均完成接口读取、会话完整性"
        "检查、同一复权因子检查及独立日线对账。未生成信号或候选，未修改SM和PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n"
        "分钟数据链路可行。15m、5m和1m每个完整交易日分别形成16、48和240根因果收线，"
        "三个周期与日线OHLCV聚合一致，可以进入完整历史数据发布与S004机制普查。\n\n"
        "本轮只证明数据接口和对账契约可用，不证明任何分钟信号或策略有效。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
