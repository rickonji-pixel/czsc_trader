from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S004_EX36"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "conditional_return_analysis",
        "event_generation",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX36 may only validate source quality and causal availability")

    dataset = protocol["dataset"]
    manifest = repo / str(dataset["market_manifest"])
    if _sha256(manifest) != str(dataset["market_manifest_sha256"]):
        raise ValueError("market evidence hash differs from frozen protocol")
    symbol = str(protocol["research_target"]["trade_symbol"])
    source = protocol["source"]
    frame = get_tushare_pro(repo / ".env").margin_detail(
        ts_code=symbol,
        start_date=str(dataset["start"]).replace("-", ""),
        end_date=str(dataset["development_cutoff"]).replace("-", ""),
    )
    if frame is None or frame.empty:
        raise ValueError("Tushare returned no margin_detail rows")
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(
        frame["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    frame = frame.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    required = [str(value) for value in source["required_fields"]]
    missing_columns = sorted(set(required) - set(frame.columns))
    duplicate_rows = int(frame.duplicated(["ts_code", "trade_date"], keep=False).sum())
    required_null_rows = (
        int(frame[required].isna().any(axis=1).sum()) if not missing_columns else -1
    )
    negative_flow_rows = int(
        (frame[["rzmre", "rzche"]].astype(float) < 0).any(axis=1).sum()
    )

    market = load_market_data(
        repo / str(dataset["market_directory"]), symbol
    ).daily.copy()
    market_dates = pd.DatetimeIndex(
        pd.to_datetime(market["dt"]).dt.normalize().drop_duplicates().sort_values()
    )
    start = pd.Timestamp(str(dataset["start"]))
    cutoff = pd.Timestamp(str(dataset["development_cutoff"]))
    market_dates = market_dates[(market_dates >= start) & (market_dates <= cutoff)]
    first_record = pd.Timestamp(frame["trade_date"].min())
    eligible_dates = market_dates[market_dates >= first_record]
    observed_dates = set(frame["trade_date"])
    covered_dates = sum(value in observed_dates for value in eligible_dates)
    coverage = covered_dates / len(eligible_dates)
    missing_sessions = [
        value.strftime("%Y-%m-%d") for value in eligible_dates if value not in observed_dates
    ]

    ordered = frame.sort_values("trade_date").copy()
    ordered["reported_net_financing"] = (
        ordered["rzmre"].astype(float) - ordered["rzche"].astype(float)
    )
    ordered["balance_change"] = ordered["rzye"].astype(float).diff()
    ordered["balance_flow_residual"] = (
        ordered["balance_change"] - ordered["reported_net_financing"]
    )
    residual = ordered["balance_flow_residual"].dropna().abs()
    gate = protocol["quality_gate"]
    passed = bool(
        not missing_columns
        and len(frame) >= int(gate["minimum_rows"])
        and coverage >= float(gate["minimum_coverage_from_first_record"])
        and duplicate_rows <= int(gate["maximum_duplicate_symbol_dates"])
        and required_null_rows <= int(gate["maximum_required_null_rows"])
        and negative_flow_rows == 0
    )
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if passed else "FAIL",
        "rows": int(len(frame)),
        "first_record": first_record.strftime("%Y-%m-%d"),
        "last_record": pd.Timestamp(frame["trade_date"].max()).strftime("%Y-%m-%d"),
        "eligible_market_sessions": int(len(eligible_dates)),
        "covered_market_sessions": int(covered_dates),
        "coverage_from_first_record": float(coverage),
        "missing_market_sessions": missing_sessions,
        "missing_columns": missing_columns,
        "duplicate_symbol_dates": duplicate_rows,
        "required_null_rows": required_null_rows,
        "negative_flow_rows": negative_flow_rows,
        "balance_flow_residual_abs_p50": float(residual.quantile(0.50)),
        "balance_flow_residual_abs_p99": float(residual.quantile(0.99)),
        "balance_flow_residual_abs_max": float(residual.max()),
        "historical_mechanism_research_allowed": passed,
        "live_preopen_arrival_proven": False,
        "deployment_allowed": False,
        "route_decision": (
            "PROCEED_TO_MARGIN_BEHAVIOR_HYPOTHESIS_CENSUS"
            if passed
            else "STOP_MARGIN_DETAIL_DATA_ROUTE"
        ),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    output = ordered.copy()
    output["trade_date"] = output["trade_date"].dt.strftime("%Y-%m-%d")
    output.to_csv(artifacts / "margin_detail.csv.gz", index=False, compression=compression)
    _write_json(artifacts / "data_quality.json", quality)

    (experiment / "03_execution.md").write_text(
        "# S004 EX36 执行\n\n"
        f"历史数据门禁：`{quality['status']}`。共{quality['rows']}条记录，"
        f"覆盖{quality['first_record']}至{quality['last_record']}；从首条记录起覆盖"
        f"{quality['covered_market_sessions']}/{quality['eligible_market_sessions']}个交易日"
        f"（{quality['coverage_from_first_record']:.2%}），核心字段缺失"
        f"{quality['required_null_rows']}条、重复{quality['duplicate_symbol_dates']}条。\n\n"
        "融资余额变化与买入额减偿还额存在调账残差，因此后续只使用交易所直接报告的"
        "买入额、偿还额构造净融资流。本轮没有生成事件或读取后续收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX36 结论\n\n"
        f"结论：`{quality['route_decision']}`。"
        "历史数据只允许生成下一交易日及以后的研究信号；本地盘前实际到达未审计，"
        "因此即使后续形成候选也不得直接部署。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": symbol,
            "development_cutoff": str(dataset["development_cutoff"]),
            "status": "COMPLETE" if passed else "FAIL",
            "route_decision": quality["route_decision"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
