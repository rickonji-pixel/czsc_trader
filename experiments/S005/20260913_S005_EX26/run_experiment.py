from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX26"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("reads_post_event_prices", "event_generation", "parameter_selection")
    ):
        raise ValueError("EX26 may only validate ETF share data")

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
    frame = pro.etf_share_size(
        ts_code=str(protocol["symbol"]),
        start_date=start.strftime("%Y%m%d"),
        end_date=cutoff.strftime("%Y%m%d"),
        fields="trade_date,ts_code,etf_name,total_share,total_size,nav,close,exchange",
    )
    data = pd.DataFrame() if frame is None else pd.DataFrame(frame)
    if data.empty:
        raise ValueError("Tushare returned no ETF share data")
    required = {"trade_date", "ts_code", "total_share"}
    missing = sorted(required.difference(data.columns))
    if missing:
        raise ValueError(f"ETF share response missing fields: {missing}")
    data["trade_date"] = pd.to_datetime(data["trade_date"], format="%Y%m%d").dt.normalize()
    data["total_share"] = pd.to_numeric(data["total_share"], errors="coerce")
    for column in ("total_size", "nav", "close"):
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    duplicates = int(data.duplicated(["trade_date", "ts_code"], keep=False).sum())
    observed_dates = pd.DatetimeIndex(data["trade_date"].unique())
    covered = calendar.intersection(observed_dates)
    unexpected = observed_dates.difference(calendar)
    total_share = data["total_share"]
    finite_positive = bool(
        total_share.notna().all()
        and np.isfinite(total_share.to_numpy(dtype=float)).all()
        and total_share.gt(0).all()
    )
    coverage = float(len(covered) / len(calendar))
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "requested_first_session": calendar.min().date().isoformat(),
        "requested_last_session": calendar.max().date().isoformat(),
        "observed_first_session": observed_dates.min().date().isoformat(),
        "observed_last_session": observed_dates.max().date().isoformat(),
        "requested_sessions": int(len(calendar)),
        "observed_sessions": int(len(observed_dates)),
        "covered_sessions": int(len(covered)),
        "session_coverage": coverage,
        "duplicate_symbol_session_rows": duplicates,
        "unexpected_noncalendar_dates": [date.date().isoformat() for date in unexpected],
        "positive_finite_total_share": finite_positive,
        "total_size_coverage": float(data["total_size"].notna().mean()),
        "nav_coverage": float(data["nav"].notna().mean()),
        "close_coverage": float(data["close"].notna().mean()),
        "api_calls": 1,
    }
    gate = protocol["quality_gate"]
    quality["passed"] = bool(
        coverage >= float(gate["minimum_session_coverage"])
        and duplicates == 0
        and unexpected.empty
        and finite_positive
        and calendar.min() in observed_dates
        and calendar.max() in observed_dates
        and quality["api_calls"] <= int(gate["maximum_api_calls"])
    )
    output = data.copy()
    output["trade_date"] = output["trade_date"].dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "etf_share_size.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    (artifacts / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S005 EX26 执行\n\n"
        f"数据门结果：`{status}`。请求{quality['requested_sessions']}个交易日，观测"
        f"{quality['observed_sessions']}日，覆盖率{quality['session_coverage']:.2%}；"
        f"总份额有效性`{quality['positive_finite_total_share']}`，重复键"
        f"{quality['duplicate_symbol_session_rows']}条。未计算份额变化、事件或收益。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_ETF_SHARE_MECHANISM_PREREGISTRATION" if quality["passed"] else "STOP_ETF_SHARE_ROUTE_ON_DATA"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX26 结论\n\n"
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
