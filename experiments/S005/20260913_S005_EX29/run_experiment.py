from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX29"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _fetch_source(pro: object, source: dict[str, str], start: pd.Timestamp, cutoff: pd.Timestamp) -> pd.DataFrame:
    endpoint = source["endpoint"]
    code = source["ts_code"]
    kwargs = {
        "ts_code": code,
        "start_date": (start - pd.Timedelta(days=7)).strftime("%Y%m%d"),
        "end_date": cutoff.strftime("%Y%m%d"),
    }
    if endpoint == "index_global":
        frame = pro.index_global(**kwargs)
        return_columns = ["trade_date", "ts_code", "close", "pct_chg"]
    elif endpoint == "us_daily_adj":
        frame = pro.us_daily_adj(**kwargs)
        return_columns = ["trade_date", "ts_code", "close", "pct_change"]
    else:
        raise ValueError(f"unsupported endpoint: {endpoint}")
    data = pd.DataFrame() if frame is None else pd.DataFrame(frame)
    missing = sorted(set(return_columns).difference(data.columns))
    if missing:
        raise ValueError(f"{endpoint}/{code} response missing fields: {missing}")
    data = data[return_columns].rename(columns={return_columns[-1]: "return_pct"}).copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], format="%Y%m%d").dt.normalize()
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data["return_pct"] = pd.to_numeric(data["return_pct"], errors="coerce")
    data["endpoint"] = endpoint
    data["role"] = source["role"]
    return data.sort_values("trade_date").reset_index(drop=True)


def _map_to_a_sessions(calendar: pd.DatetimeIndex, source: pd.DataFrame) -> pd.DataFrame:
    left = pd.DataFrame({"a_share_session": calendar}).sort_values("a_share_session")
    right = source.rename(columns={"trade_date": "us_trade_date"}).sort_values("us_trade_date")
    mapped = pd.merge_asof(
        left,
        right,
        left_on="a_share_session",
        right_on="us_trade_date",
        direction="backward",
        allow_exact_matches=False,
    )
    mapped["calendar_lag_days"] = (mapped["a_share_session"] - mapped["us_trade_date"]).dt.days
    return mapped


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("reads_post_event_prices", "event_generation", "parameter_selection")):
        raise ValueError("EX29 may only validate global catalyst data")

    calendar_spec = protocol["calendar"]
    calendar_manifest = repo / str(calendar_spec["manifest"])
    if raw_file_sha256(calendar_manifest) != calendar_spec["manifest_sha256"]:
        raise ValueError("588080 calendar manifest differs from frozen evidence")
    start = pd.Timestamp(protocol["development_start"])
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    market = load_market_data(repo / "data/raw", str(protocol["symbol"])).daily
    calendar = pd.DatetimeIndex(pd.to_datetime(market["dt"]).dt.normalize())
    calendar = calendar[(calendar >= start) & (calendar <= cutoff)]

    pro = get_tushare_pro(repo / ".env")
    mapped_parts: list[pd.DataFrame] = []
    source_quality: list[dict[str, object]] = []
    gate = protocol["quality_gate"]
    max_lag = int(protocol["mapping_rule"]["maximum_calendar_lag_days"])
    for raw_source in protocol["sources"]:
        source = dict(raw_source)
        data = _fetch_source(pro, source, start, cutoff)
        duplicate_rows = int(data.duplicated(["trade_date", "ts_code"], keep=False).sum())
        mapped = _map_to_a_sessions(calendar, data)
        mapped["source_code"] = source["ts_code"]
        valid = mapped["us_trade_date"].notna()
        finite = valid & np.isfinite(mapped["close"]) & np.isfinite(mapped["return_pct"])
        causal = valid & mapped["us_trade_date"].lt(mapped["a_share_session"])
        timely = valid & mapped["calendar_lag_days"].le(max_lag)
        usable = finite & causal & timely
        quality = {
            "endpoint": source["endpoint"],
            "ts_code": source["ts_code"],
            "role": source["role"],
            "source_rows": int(len(data)),
            "source_first_session": data["trade_date"].min().date().isoformat(),
            "source_last_session": data["trade_date"].max().date().isoformat(),
            "duplicate_source_session_rows": duplicate_rows,
            "mapped_sessions": int(usable.sum()),
            "mapped_session_coverage": float(usable.mean()),
            "noncausal_mappings": int((valid & ~causal).sum()),
            "over_maximum_lag_mappings": int((valid & ~timely).sum()),
            "maximum_observed_lag_days": int(mapped.loc[valid, "calendar_lag_days"].max()),
            "finite_close_and_return": bool(finite[valid].all()),
        }
        quality["passed"] = bool(
            duplicate_rows == 0
            and quality["mapped_session_coverage"] >= float(gate["minimum_mapped_session_coverage_per_source"])
            and quality["noncausal_mappings"] == 0
            and quality["over_maximum_lag_mappings"] == 0
            and quality["finite_close_and_return"]
        )
        source_quality.append(quality)
        mapped_parts.append(mapped)

    panel = pd.concat(mapped_parts, ignore_index=True)
    panel = panel.sort_values(["a_share_session", "source_code"]).reset_index(drop=True)
    panel["a_share_session"] = panel["a_share_session"].dt.strftime("%Y-%m-%d")
    panel["us_trade_date"] = panel["us_trade_date"].dt.strftime("%Y-%m-%d")
    panel.to_csv(
        artifacts / "global_technology_panel.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "requested_a_share_sessions": int(len(calendar)),
        "api_calls": int(len(protocol["sources"])),
        "source_quality": source_quality,
    }
    quality["passed"] = bool(
        quality["api_calls"] <= int(gate["maximum_api_calls"])
        and all(item["passed"] for item in source_quality)
    )
    (artifacts / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    status = "PASS" if quality["passed"] else "FAIL"
    summary = "；".join(
        f"{item['ts_code']} {item['mapped_sessions']}/{quality['requested_a_share_sessions']}"
        for item in source_quality
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX29 执行\n\n"
        f"数据门结果：`{status}`。映射覆盖：{summary}。"
        f"非因果映射{sum(int(item['noncausal_mappings']) for item in source_quality)}条，"
        f"超过{max_lag}日延迟{sum(int(item['over_maximum_lag_mappings']) for item in source_quality)}条。"
        "未生成事件，未读取588080未来收益。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_GLOBAL_TECH_MECHANISM_PREREGISTRATION" if quality["passed"] else "STOP_GLOBAL_TECH_ROUTE_ON_DATA"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX29 结论\n\n"
        f"裁决：`{decision}`。没有创建候选、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "status": status,
            "reads_post_event_prices": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
