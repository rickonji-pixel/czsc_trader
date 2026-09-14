from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260914_S005_EX64"
MONEYFLOW_FIELDS = (
    "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,"
    "sell_md_amount,buy_lg_amount,sell_lg_amount,buy_elg_amount,"
    "sell_elg_amount,net_mf_amount"
)


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


def _membership_panel(
    memberships: pd.DataFrame,
    calendar: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, int]:
    rows: list[pd.DataFrame] = []
    for item in memberships.itertuples(index=False):
        start = max(pd.Timestamp(item.in_date), calendar[0])
        end = calendar[-1] if pd.isna(item.out_date) else min(pd.Timestamp(item.out_date), calendar[-1])
        sessions = calendar[(calendar >= start) & (calendar <= end)]
        if sessions.empty:
            continue
        rows.append(
            pd.DataFrame(
                {
                    "trade_date": sessions,
                    "ts_code": str(item.ts_code),
                    "l3_code": str(item.l3_code),
                    "l3_name": str(item.l3_name),
                    "membership_in_date": pd.Timestamp(item.in_date),
                    "membership_out_date": item.out_date,
                    "membership_is_new": str(item.is_new),
                }
            )
        )
    if not rows:
        raise ValueError("no semiconductor membership is active in the development pool")
    panel = pd.concat(rows, ignore_index=True)
    duplicate_member_days = int(panel.duplicated(["trade_date", "ts_code"], keep=False).sum())
    panel = panel.drop_duplicates(["trade_date", "ts_code"]).sort_values(
        ["trade_date", "ts_code"]
    )
    return panel.reset_index(drop=True), duplicate_member_days


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
        for key in (
            "reads_target_forward_returns",
            "event_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX64 is a data-only gate")

    calendar_spec = protocol["calendar_manifest"]
    calendar_path = repo / str(calendar_spec["path"])
    if raw_file_sha256(calendar_path) != calendar_spec["sha256"]:
        raise ValueError("588080 research calendar differs from frozen protocol")
    star_spec = protocol["star50_reference"]
    star_path = repo / str(star_spec["path"])
    if raw_file_sha256(star_path) != star_spec["sha256"]:
        raise ValueError("STAR50 point-in-time reference differs from frozen protocol")

    start = pd.Timestamp(str(protocol["evaluation_start"]))
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    market = load_market_data(repo / "data/raw", str(protocol["symbol"])).daily
    calendar = pd.DatetimeIndex(pd.to_datetime(market["dt"]).dt.normalize())
    calendar = calendar[(calendar >= start) & (calendar <= cutoff)]
    if len(calendar) < int(protocol["quality_gate"]["minimum_usable_sessions"]):
        raise ValueError("frozen 588080 calendar is shorter than the preregistered minimum")

    sources = protocol["sources"]
    pro = get_tushare_pro(repo / ".env")
    membership_frames = []
    api_calls = 0
    for is_new in ("Y", "N"):
        frame = _request(
            pro.index_member_all,
            l2_code=str(sources["industry_l2_code"]),
            is_new=is_new,
        )
        api_calls += 1
        if not frame.empty:
            membership_frames.append(frame)
    if not membership_frames:
        raise ValueError("Tushare returned no semiconductor memberships")
    memberships = pd.concat(membership_frames, ignore_index=True)
    required_membership = {
        "l2_code",
        "l2_name",
        "l3_code",
        "l3_name",
        "ts_code",
        "name",
        "in_date",
        "out_date",
        "is_new",
    }
    missing = required_membership.difference(memberships.columns)
    if missing:
        raise ValueError(f"membership response missing columns: {sorted(missing)}")
    memberships = memberships.loc[
        memberships["l2_code"].eq(str(sources["industry_l2_code"]))
    ].copy()
    memberships["in_date"] = pd.to_datetime(memberships["in_date"], format="%Y%m%d")
    memberships["out_date"] = pd.to_datetime(
        memberships["out_date"], format="%Y%m%d", errors="coerce"
    )
    if memberships["in_date"].isna().any():
        raise ValueError("membership contains an invalid in_date")
    if (
        memberships["out_date"].notna()
        & memberships["out_date"].lt(memberships["in_date"])
    ).any():
        raise ValueError("membership contains an out_date before in_date")
    memberships = memberships.drop_duplicates(
        ["ts_code", "in_date", "out_date"], keep="first"
    ).sort_values(["ts_code", "in_date"])
    membership_panel, duplicate_member_days = _membership_panel(memberships, calendar)

    flow_frames: list[pd.DataFrame] = []
    codes = sorted(membership_panel["ts_code"].unique())
    for position, code in enumerate(codes, start=1):
        flow = _request(
            pro.moneyflow,
            ts_code=code,
            start_date=start.strftime("%Y%m%d"),
            end_date=cutoff.strftime("%Y%m%d"),
            fields=MONEYFLOW_FIELDS,
        )
        api_calls += 1
        if not flow.empty:
            flow_frames.append(flow)
        if position % 25 == 0 or position == len(codes):
            print(f"moneyflow {position}/{len(codes)}", flush=True)
    if not flow_frames:
        raise ValueError("Tushare returned no semiconductor member moneyflow")
    moneyflow = pd.concat(flow_frames, ignore_index=True)
    moneyflow["trade_date"] = pd.to_datetime(moneyflow["trade_date"], format="%Y%m%d")
    duplicate_moneyflow_rows = int(
        moneyflow.duplicated(["trade_date", "ts_code"], keep=False).sum()
    )
    moneyflow = moneyflow.drop_duplicates(["trade_date", "ts_code"])
    amount_columns = [column for column in MONEYFLOW_FIELDS.split(",") if column.endswith("_amount")]
    for column in amount_columns:
        moneyflow[column] = pd.to_numeric(moneyflow[column], errors="coerce")

    panel = membership_panel.merge(
        moneyflow,
        on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    panel["observed_moneyflow"] = panel["net_mf_amount"].notna()
    panel["positive_moneyflow"] = panel["net_mf_amount"].gt(0.0)
    gross_columns = [column for column in amount_columns if column != "net_mf_amount"]
    panel["gross_order_amount"] = panel[gross_columns].sum(axis=1, min_count=1)

    by_session = panel.groupby("trade_date", sort=True, observed=True).agg(
        active_members=("ts_code", "nunique"),
        observed_members=("observed_moneyflow", "sum"),
        member_coverage=("observed_moneyflow", "mean"),
        positive_member_ratio=("positive_moneyflow", "mean"),
        net_mf_amount=("net_mf_amount", "sum"),
        gross_order_amount=("gross_order_amount", "sum"),
    )
    by_session["net_flow_ratio"] = by_session["net_mf_amount"].div(
        by_session["gross_order_amount"].replace(0.0, np.nan)
    )

    star = pd.read_csv(star_path, usecols=["con_code"])
    star_codes = set(star["con_code"].astype(str))
    industry_codes = set(codes)
    outside_star50 = sorted(industry_codes.difference(star_codes))
    historical_memberships = int(memberships["is_new"].eq("N").sum())

    direct_sources: dict[str, dict[str, object]] = {}
    for label, method, code in (
        ("THS", pro.moneyflow_ind_ths, str(sources["direct_ths_code"])),
        ("DC", pro.moneyflow_ind_dc, str(sources["direct_dc_code"])),
    ):
        direct = _request(
            method,
            ts_code=code,
            start_date=start.strftime("%Y%m%d"),
            end_date=cutoff.strftime("%Y%m%d"),
        )
        api_calls += 1
        dates = direct["trade_date"].astype(str) if not direct.empty else pd.Series(dtype=str)
        direct_sources[label] = {
            "code": code,
            "rows": int(len(direct)),
            "first_date": None if direct.empty else str(dates.min()),
            "last_date": None if direct.empty else str(dates.max()),
            "development_calendar_coverage": float(len(set(dates)) / len(calendar)),
        }

    gate = protocol["quality_gate"]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "calendar_sessions": int(len(calendar)),
        "usable_sessions": int(len(by_session)),
        "membership_rows": int(len(memberships)),
        "historical_memberships": historical_memberships,
        "industry_member_union": int(len(industry_codes)),
        "members_outside_star50": int(len(outside_star50)),
        "members_outside_star50_ratio": float(len(outside_star50) / len(industry_codes)),
        "panel_rows": int(len(panel)),
        "member_moneyflow_coverage": float(panel["observed_moneyflow"].mean()),
        "daily_moneyflow_coverage_p10": float(by_session["member_coverage"].quantile(0.10)),
        "duplicate_member_days": duplicate_member_days,
        "duplicate_moneyflow_rows": duplicate_moneyflow_rows,
        "api_calls": api_calls,
        "direct_vendor_sources": direct_sources,
        "causal_use": str(protocol["availability"]["conservative_use"]),
    }
    evidence["passed"] = bool(
        evidence["usable_sessions"] >= int(gate["minimum_usable_sessions"])
        and evidence["member_moneyflow_coverage"]
        >= float(gate["minimum_member_moneyflow_coverage"])
        and evidence["daily_moneyflow_coverage_p10"]
        >= float(gate["minimum_daily_moneyflow_coverage_p10"])
        and evidence["members_outside_star50"]
        >= int(gate["minimum_members_outside_star50"])
        and historical_memberships >= int(gate["minimum_historical_memberships"])
        and duplicate_member_days <= int(gate["maximum_duplicate_member_days"])
        and duplicate_moneyflow_rows <= int(gate["maximum_duplicate_moneyflow_rows"])
        and api_calls <= int(gate["maximum_api_calls"])
    )

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    membership_out = memberships.copy()
    membership_out["in_date"] = membership_out["in_date"].dt.strftime("%Y-%m-%d")
    membership_out["out_date"] = membership_out["out_date"].dt.strftime("%Y-%m-%d")
    membership_out.to_csv(
        artifacts / "semiconductor_memberships.csv",
        index=False,
        lineterminator="\n",
    )
    panel.to_csv(
        artifacts / "semiconductor_moneyflow_panel.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    by_session.reset_index().to_csv(
        artifacts / "industry_daily_factors.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    pd.DataFrame({"ts_code": outside_star50}).to_csv(
        artifacts / "members_outside_star50.csv",
        index=False,
        lineterminator="\n",
    )
    _write(artifacts / "data_gate_evidence.json", evidence)

    status = "PASS" if evidence["passed"] else "FAIL"
    decision = (
        "PROCEED_TO_INDUSTRY_FLOW_MECHANISM_PREREGISTRATION"
        if evidence["passed"]
        else "STOP_INDUSTRY_FLOW_ROUTE_ON_DATA"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX64 执行\n\n"
        f"状态：`{status}`。历史成员并集{evidence['industry_member_union']}只，其中"
        f"{evidence['members_outside_star50']}只不属于历史科创50成员并集；形成"
        f"{evidence['usable_sessions']}个交易日、{evidence['panel_rows']}条成员日记录。"
        f"资金流总体覆盖{evidence['member_moneyflow_coverage']:.2%}，逐日覆盖P10为"
        f"{evidence['daily_moneyflow_coverage_p10']:.2%}，API调用{evidence['api_calls']}次。\n\n"
        "本轮没有定义事件或读取588080后续收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX64 结论\n\n"
        f"裁决：`{decision}`。供应商直接行业流仅覆盖"
        f"{direct_sources['DC']['development_calendar_coverage']:.2%}（DC）和"
        f"{direct_sources['THS']['development_calendar_coverage']:.2%}（THS）的开发池；"
        "历史成分重建面板覆盖完整开发池并扩展到科创50之外的行业资金。"
        "通过只代表数据与因果契约可用，下一轮仍须先登记竞争解释并做无收益机制设计。"
        "没有创建候选、修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": status,
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "reads_target_forward_returns": False,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
