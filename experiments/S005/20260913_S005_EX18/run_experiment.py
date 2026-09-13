from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX18"


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


def _request(method, **kwargs) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            value = method(**kwargs)
            return pd.DataFrame() if value is None else pd.DataFrame(value)
        except Exception as error:  # pragma: no cover - external retry
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _month_windows(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    return [
        (max(start, period.start_time.normalize()), min(end, period.end_time.normalize()))
        for period in pd.period_range(start, end, freq="M")
    ]


def _membership_panel(snapshots: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    dates = sorted(pd.DatetimeIndex(snapshots["trade_date"].unique()))
    for index, snapshot_date in enumerate(dates):
        next_snapshot = dates[index + 1] if index + 1 < len(dates) else None
        sessions = calendar[calendar > snapshot_date]
        if next_snapshot is not None:
            sessions = sessions[sessions <= next_snapshot]
        members = snapshots.loc[
            snapshots["trade_date"].eq(snapshot_date), ["con_code", "weight"]
        ].copy()
        if sessions.empty:
            continue
        repeated = members.loc[members.index.repeat(len(sessions))].reset_index(drop=True)
        repeated["dt"] = np.tile(sessions.to_numpy(), len(members))
        repeated["snapshot_date"] = snapshot_date
        frames.append(repeated)
    return pd.concat(frames, ignore_index=True).sort_values(["dt", "con_code"])


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("reads_post_event_prices", "event_generation", "parameter_selection")
    ):
        raise ValueError("EX18 may only validate source data")

    dataset = protocol["dataset"]
    calendar_manifest = repo / str(dataset["calendar_manifest"])
    if raw_file_sha256(calendar_manifest) != dataset["calendar_manifest_sha256"]:
        raise ValueError("588080 calendar manifest differs from frozen evidence")
    start = pd.Timestamp(dataset["start"])
    cutoff = pd.Timestamp(dataset["development_cutoff"])
    market = load_market_data(repo / "data/raw", str(protocol["trade_symbol"])).daily
    calendar = pd.DatetimeIndex(pd.to_datetime(market["dt"]).dt.normalize())
    calendar = calendar[(calendar >= start) & (calendar <= cutoff)]

    pro = get_tushare_pro(repo / ".env")
    snapshots: list[pd.DataFrame] = []
    api_calls = 0
    for left, right in _month_windows(start, cutoff):
        frame = _request(
            pro.index_weight,
            index_code=str(protocol["information_symbol"]),
            start_date=left.strftime("%Y%m%d"),
            end_date=right.strftime("%Y%m%d"),
            fields="index_code,con_code,trade_date,weight",
        )
        api_calls += 1
        if not frame.empty:
            snapshots.append(frame)
    if not snapshots:
        raise ValueError("Tushare returned no STAR50 index weights")
    weight = pd.concat(snapshots, ignore_index=True).drop_duplicates()
    weight["trade_date"] = pd.to_datetime(weight["trade_date"], format="%Y%m%d")
    weight["weight"] = pd.to_numeric(weight["weight"], errors="raise")
    weight = weight.sort_values(["trade_date", "con_code"]).reset_index(drop=True)
    membership = _membership_panel(weight, calendar)
    if membership.empty:
        raise ValueError("point-in-time membership panel is empty")

    daily_frames: list[pd.DataFrame] = []
    flow_frames: list[pd.DataFrame] = []
    codes = sorted(membership["con_code"].unique())
    for position, code in enumerate(codes, start=1):
        daily = _request(
            pro.daily,
            ts_code=code,
            start_date=start.strftime("%Y%m%d"),
            end_date=cutoff.strftime("%Y%m%d"),
            fields="ts_code,trade_date,pct_chg,vol,amount",
        )
        flow = _request(
            pro.moneyflow,
            ts_code=code,
            start_date=start.strftime("%Y%m%d"),
            end_date=cutoff.strftime("%Y%m%d"),
            fields=(
                "ts_code,trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount,"
                "buy_elg_amount,sell_elg_amount"
            ),
        )
        api_calls += 2
        if not daily.empty:
            daily_frames.append(daily)
        if not flow.empty:
            flow_frames.append(flow)
        if position % 20 == 0 or position == len(codes):
            print(f"constituents {position}/{len(codes)}", flush=True)
    if not daily_frames or not flow_frames:
        raise ValueError("Tushare returned incomplete constituent source families")

    daily = pd.concat(daily_frames, ignore_index=True)
    flow = pd.concat(flow_frames, ignore_index=True)
    for frame in (daily, flow):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
    daily_duplicates = int(daily.duplicated(["trade_date", "ts_code"], keep=False).sum())
    flow_duplicates = int(flow.duplicated(["trade_date", "ts_code"], keep=False).sum())
    daily = daily.drop_duplicates(["trade_date", "ts_code"])
    flow = flow.drop_duplicates(["trade_date", "ts_code"])
    panel = membership.merge(
        daily.rename(columns={"trade_date": "dt", "ts_code": "con_code"}),
        on=["dt", "con_code"],
        how="left",
        validate="one_to_one",
    ).merge(
        flow.rename(columns={"trade_date": "dt", "ts_code": "con_code"}),
        on=["dt", "con_code"],
        how="left",
        validate="one_to_one",
    )
    panel["observed_daily"] = panel["pct_chg"].notna()
    panel["observed_moneyflow"] = panel["net_mf_amount"].notna()
    by_session = panel.groupby("dt", sort=True, observed=True).agg(
        member_count=("con_code", "nunique"),
        weight_sum=("weight", "sum"),
        daily_coverage=("observed_daily", "mean"),
        moneyflow_coverage=("observed_moneyflow", "mean"),
    )
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "first_snapshot": weight["trade_date"].min().date().isoformat(),
        "last_snapshot": weight["trade_date"].max().date().isoformat(),
        "snapshot_count": int(weight["trade_date"].nunique()),
        "usable_sessions": int(panel["dt"].nunique()),
        "constituent_union": len(codes),
        "panel_rows": int(len(panel)),
        "member_count_min": int(by_session["member_count"].min()),
        "member_count_max": int(by_session["member_count"].max()),
        "weight_sum_min": float(by_session["weight_sum"].min()),
        "weight_sum_max": float(by_session["weight_sum"].max()),
        "daily_coverage_ratio": float(panel["observed_daily"].mean()),
        "moneyflow_coverage_ratio": float(panel["observed_moneyflow"].mean()),
        "moneyflow_daily_coverage_p10": float(by_session["moneyflow_coverage"].quantile(0.1)),
        "daily_duplicate_rows": daily_duplicates,
        "moneyflow_duplicate_rows": flow_duplicates,
        "causal_membership": bool((panel["snapshot_date"] < panel["dt"]).all()),
        "api_calls": api_calls,
    }
    quality["passed"] = bool(
        quality["member_count_min"] == gate["members_per_session"]
        and quality["member_count_max"] == gate["members_per_session"]
        and quality["weight_sum_min"] >= gate["weight_sum_min"]
        and quality["weight_sum_max"] <= gate["weight_sum_max"]
        and quality["daily_coverage_ratio"] >= gate["minimum_daily_coverage_ratio"]
        and quality["moneyflow_coverage_ratio"] >= gate["minimum_moneyflow_coverage_ratio"]
        and quality["moneyflow_daily_coverage_p10"] >= gate["minimum_moneyflow_daily_p10"]
        and daily_duplicates == 0
        and flow_duplicates == 0
        and quality["causal_membership"]
        and api_calls <= gate["maximum_api_calls"]
    )

    panel_out = panel.copy()
    panel_out["dt"] = panel_out["dt"].dt.strftime("%Y-%m-%d")
    panel_out["snapshot_date"] = panel_out["snapshot_date"].dt.strftime("%Y-%m-%d")
    panel_out.to_csv(
        artifacts / "constituent_panel.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    coverage = by_session.reset_index()
    coverage["dt"] = coverage["dt"].dt.strftime("%Y-%m-%d")
    coverage.to_csv(artifacts / "session_quality.csv", index=False, lineterminator="\n")
    _write(artifacts / "data_quality.json", quality)

    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S005 EX18 执行\n\n"
        f"数据门结果：`{status}`。取得{quality['snapshot_count']}个权重快照、"
        f"{quality['usable_sessions']}个可用交易日、历史成分并集{quality['constituent_union']}只、"
        f"{quality['panel_rows']}条成员日记录。日线总体覆盖{quality['daily_coverage_ratio']:.2%}，"
        f"资金流总体覆盖{quality['moneyflow_coverage_ratio']:.2%}，逐日覆盖P10为"
        f"{quality['moneyflow_daily_coverage_p10']:.2%}。共调用{quality['api_calls']}次。\n\n"
        "本轮没有定义事件或读取588080事件后收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "历史时点成分股横截面可进入独立机制预注册。"
        if quality["passed"]
        else "数据或OPC维护成本门未通过，当前不得进入收益研究。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX18 结论\n\n"
        f"裁决：`{'PROCEED_TO_CONSTITUENT_MECHANISM_PREREGISTRATION' if quality['passed'] else 'STOP_CONSTITUENT_ROUTE_ON_DATA'}`。"
        f"{conclusion}没有创建候选、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "reads_post_event_prices": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
