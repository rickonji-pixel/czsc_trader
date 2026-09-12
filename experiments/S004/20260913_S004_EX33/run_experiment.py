from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S004_EX33"


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


def _fetch_us_daily(pro, symbols: list[str], start: str, end: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    fields = "ts_code,trade_date,close,pre_close,pct_change,vol,amount"
    for symbol in symbols:
        frame = pro.us_daily(
            ts_code=symbol,
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            fields=fields,
        )
        if frame is None or frame.empty:
            raise ValueError(f"Tushare returned no US daily rows for {symbol}")
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True)
    result["trade_date"] = pd.to_datetime(
        result["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    return result.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


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
        raise ValueError("EX33 may only validate source coverage and causal mapping")

    dataset = protocol["dataset"]
    source = protocol["sources"]["us_daily"]
    symbols = [str(value) for value in source["symbols"]]
    required_columns = [str(value) for value in source["fields"]]
    us_daily = _fetch_us_daily(
        get_tushare_pro(repo / ".env"),
        symbols,
        str(dataset["start"]),
        str(dataset["development_cutoff"]),
    )
    missing_columns = sorted(set(required_columns) - set(us_daily.columns))
    duplicate_rows = int(us_daily.duplicated(["ts_code", "trade_date"], keep=False).sum())
    null_rows = int(us_daily[required_columns].isna().any(axis=1).sum()) if not missing_columns else -1
    nonpositive_prices = int(
        (us_daily[["close", "pre_close"]].astype(float) <= 0).any(axis=1).sum()
    )

    dates_by_symbol = {
        symbol: set(us_daily.loc[us_daily["ts_code"].eq(symbol), "trade_date"])
        for symbol in symbols
    }
    reference_dates = dates_by_symbol["QQQ"]
    pairwise_coverage = {
        symbol: len(reference_dates & dates) / len(reference_dates)
        for symbol, dates in dates_by_symbol.items()
    }
    common_dates = sorted(set.intersection(*(dates_by_symbol[symbol] for symbol in symbols)))

    intraday = load_intraday_research_data(
        repo / str(dataset["directory"]), str(protocol["research_target"]["trade_symbol"])
    ).frames["1m"]
    a_share_dates = pd.DataFrame(
        {
            "a_share_date": sorted(
                pd.to_datetime(intraday["Date"]).dt.normalize().drop_duplicates()
            )
        }
    )
    start = pd.Timestamp(str(dataset["start"]))
    cutoff = pd.Timestamp(str(dataset["development_cutoff"]))
    a_share_dates = a_share_dates.loc[
        a_share_dates["a_share_date"].between(start, cutoff)
    ].reset_index(drop=True)
    us_sessions = pd.DataFrame({"us_trade_date": common_dates})
    mapping = pd.merge_asof(
        a_share_dates.sort_values("a_share_date"),
        us_sessions.sort_values("us_trade_date"),
        left_on="a_share_date",
        right_on="us_trade_date",
        direction="backward",
        allow_exact_matches=False,
    )
    mapping["mapping_age_calendar_days"] = (
        mapping["a_share_date"] - mapping["us_trade_date"]
    ).dt.days
    mapping_coverage = float(mapping["us_trade_date"].notna().mean())
    mapped_ages = mapping["mapping_age_calendar_days"].dropna()
    mapping_age_p99 = float(mapped_ages.quantile(0.99))

    gate = protocol["quality_gate"]
    passed = bool(
        not missing_columns
        and duplicate_rows == 0
        and null_rows == 0
        and nonpositive_prices == 0
        and len(dates_by_symbol) == int(gate["required_us_symbols"])
        and min(pairwise_coverage.values()) >= float(gate["minimum_pairwise_date_coverage"])
        and mapping_coverage >= float(gate["minimum_a_share_session_mapping_coverage"])
        and mapping_age_p99 <= float(gate["maximum_mapping_age_p99_calendar_days"])
    )
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if passed else "FAIL",
        "rows": int(len(us_daily)),
        "symbols": {
            symbol: {
                "rows": int(len(us_daily.loc[us_daily["ts_code"].eq(symbol)])),
                "first": min(dates_by_symbol[symbol]).strftime("%Y-%m-%d"),
                "last": max(dates_by_symbol[symbol]).strftime("%Y-%m-%d"),
                "qqq_date_coverage": float(pairwise_coverage[symbol]),
            }
            for symbol in symbols
        },
        "missing_columns": missing_columns,
        "duplicate_symbol_dates": duplicate_rows,
        "rows_with_required_null": null_rows,
        "rows_with_nonpositive_price": nonpositive_prices,
        "common_us_sessions": int(len(common_dates)),
        "a_share_sessions": int(len(mapping)),
        "a_share_mapping_coverage": mapping_coverage,
        "mapping_age_p99_calendar_days": mapping_age_p99,
        "mapping_age_max_calendar_days": int(mapped_ages.max()),
        "historical_mechanism_research_allowed": passed,
        "live_preopen_arrival_proven": False,
        "deployment_allowed": False,
        "route_decision": (
            "PROCEED_TO_OVERSEAS_TRANSMISSION_CENSUS"
            if passed
            else "STOP_OVERSEAS_TRANSMISSION_DATA_ROUTE"
        ),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    us_out = us_daily.copy()
    us_out["trade_date"] = us_out["trade_date"].dt.strftime("%Y-%m-%d")
    us_out.to_csv(artifacts / "us_daily.csv.gz", index=False, compression=compression)
    mapping_out = mapping.copy()
    mapping_out["a_share_date"] = mapping_out["a_share_date"].dt.strftime("%Y-%m-%d")
    mapping_out["us_trade_date"] = mapping_out["us_trade_date"].dt.strftime("%Y-%m-%d")
    mapping_out.to_csv(artifacts / "session_mapping.csv.gz", index=False, compression=compression)
    _write_json(artifacts / "data_quality.json", quality)

    status_text = "通过" if passed else "失败"
    (experiment / "03_execution.md").write_text(
        "# S004 EX33 执行\n\n"
        f"历史数据门禁：`{quality['status']}`。四个美股标的合计{quality['rows']}条、"
        f"共同交易日{quality['common_us_sessions']}个；映射{quality['a_share_sessions']}个"
        f"A股交易日，覆盖率{quality['a_share_mapping_coverage']:.2%}，映射年龄P99为"
        f"{quality['mapping_age_p99_calendar_days']:.0f}个日历日。\n\n"
        f"历史机制研究门禁{status_text}；盘前实际到达仍未证明，因此禁止部署。"
        "本轮没有生成事件或读取588080后续收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX33 结论\n\n"
        f"结论：`{quality['route_decision']}`。"
        "QQQ、SMH、SOXX与NVDA可以作为海外科技信息传导的历史输入；下一轮只做"
        "事件密度与竞争假设定义。美股日线在A股开盘前的稳定到达必须通过独立前瞻"
        "审计，未通过前不得进入PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
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

