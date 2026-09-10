from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX30"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _request_with_retry(method, **kwargs) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            frame = method(**kwargs)
            return frame if frame is not None else pd.DataFrame()
        except Exception as error:  # pragma: no cover - network retry
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _build_membership_panel(
    snapshots: pd.DataFrame, calendar: pd.DatetimeIndex
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    snapshot_dates = sorted(pd.DatetimeIndex(snapshots["trade_date"].unique()))
    for index, snapshot_date in enumerate(snapshot_dates):
        next_snapshot = snapshot_dates[index + 1] if index + 1 < len(snapshot_dates) else None
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
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["dt", "con_code"]).reset_index(drop=True)


def _fetch_daily(
    pro, codes: list[str], start: str, end: str, batch_size: int, fields: str
) -> tuple[pd.DataFrame, int]:
    frames: list[pd.DataFrame] = []
    batches = _chunks(codes, batch_size)
    for index, batch in enumerate(batches, start=1):
        frame = _request_with_retry(
            pro.daily,
            ts_code=",".join(batch),
            start_date=start,
            end_date=end,
            fields=fields,
        )
        if not frame.empty:
            frames.append(frame)
        if index % 50 == 0 or index == len(batches):
            print(f"daily batches {index}/{len(batches)}", flush=True)
    if not frames:
        return pd.DataFrame(), len(batches)
    return pd.concat(frames, ignore_index=True), len(batches)


def _fetch_suspensions(
    pro, codes: list[str], start: str, end: str, batch_size: int
) -> tuple[pd.DataFrame, int]:
    frames: list[pd.DataFrame] = []
    batches = _chunks(codes, batch_size)
    for batch in batches:
        frame = _request_with_retry(
            pro.suspend_d,
            ts_code=",".join(batch),
            start_date=start,
            end_date=end,
            suspend_type="S",
        )
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return (
            pd.DataFrame(columns=["ts_code", "trade_date", "suspend_type", "suspend_timing"]),
            len(batches),
        )
    return pd.concat(frames, ignore_index=True), len(batches)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "conditional_return_analysis",
            "breadth_feature_generation",
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX30 may only construct the frozen constituent daily panel")

    source = protocol["source"]
    gate_experiment = repo_root / "experiments" / source["gate_experiment"]
    member_experiment = repo_root / "experiments" / source["membership_experiment"]
    validate_experiment_archive(gate_experiment)
    validate_experiment_archive(member_experiment)
    expected = {
        gate_experiment / "experiment_manifest.json": source["gate_manifest_sha256"],
        gate_experiment / "artifacts" / "reviewed_data_quality.json": source[
            "gate_quality_sha256"
        ],
        member_experiment / "artifacts" / "index_weight_snapshots.csv.gz": source[
            "membership_file_sha256"
        ],
        repo_root / source["calendar_manifest"]: source["calendar_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    gate = _read_json(gate_experiment / "artifacts" / "reviewed_data_quality.json")
    if not gate["passed"]:
        raise ValueError("EX29 reviewed data gate did not pass")

    dataset = protocol["dataset"]
    start = pd.Timestamp(dataset["start"])
    cutoff = pd.Timestamp(dataset["development_cutoff"])
    master = load_market_data(repo_root / "data" / "raw", "510500.SH").daily
    calendar = pd.DatetimeIndex(pd.to_datetime(master["dt"]).dt.normalize())
    calendar = calendar[(calendar >= start) & (calendar <= cutoff)]
    snapshots = pd.read_csv(
        member_experiment / "artifacts" / "index_weight_snapshots.csv.gz"
    )
    snapshots["trade_date"] = pd.to_datetime(snapshots["trade_date"])
    membership = _build_membership_panel(snapshots, calendar)
    if membership.empty:
        raise ValueError("point-in-time membership panel is empty")

    fetch = protocol["fetch"]
    codes = sorted(membership["con_code"].unique())
    pro = get_tushare_pro(repo_root / ".env")
    start_api = start.strftime("%Y%m%d")
    end_api = cutoff.strftime("%Y%m%d")
    daily, daily_calls = _fetch_daily(
        pro,
        codes,
        start_api,
        end_api,
        int(fetch["symbols_per_daily_request"]),
        ",".join(fetch["daily_fields"]),
    )
    if daily.empty:
        raise ValueError("Tushare returned no constituent daily rows")
    daily["trade_date"] = pd.to_datetime(
        daily["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    daily_duplicate_rows = int(daily.duplicated(["trade_date", "ts_code"], keep=False).sum())
    daily = daily.drop_duplicates(["trade_date", "ts_code"])

    panel = membership.merge(
        daily.rename(columns={"trade_date": "dt", "ts_code": "con_code"}),
        on=["dt", "con_code"],
        how="left",
        validate="one_to_one",
    )
    panel["observed"] = panel["close"].notna()
    missing_codes = sorted(panel.loc[~panel["observed"], "con_code"].unique())
    suspensions, suspension_calls = _fetch_suspensions(
        pro,
        missing_codes,
        start_api,
        end_api,
        int(fetch["symbols_per_suspension_request"]),
    )
    if not suspensions.empty:
        suspensions["trade_date"] = pd.to_datetime(
            suspensions["trade_date"].astype(str), format="%Y%m%d"
        ).dt.normalize()
        full_day = suspensions["suspend_timing"].isna() | suspensions[
            "suspend_timing"
        ].astype(str).str.strip().isin(["", "None", "nan"])
        suspension_keys = set(
            zip(
                suspensions.loc[full_day, "trade_date"],
                suspensions.loc[full_day, "ts_code"],
                strict=False,
            )
        )
    else:
        suspension_keys = set()
    panel["suspended"] = [
        (dt, code) in suspension_keys if not observed else False
        for dt, code, observed in zip(panel["dt"], panel["con_code"], panel["observed"], strict=False)
    ]
    unexplained = panel.loc[~panel["observed"] & ~panel["suspended"], ["dt", "con_code"]]

    session_counts = membership.groupby("dt")["con_code"].nunique()
    observed = panel.loc[panel["observed"]]
    finite_returns = bool(np.isfinite(observed["pct_chg"].to_numpy(dtype=float)).all())
    nonnegative_activity = bool((observed[["vol", "amount"]] >= 0).all().all())
    causal_membership = bool((membership["snapshot_date"] < membership["dt"]).all())
    output = panel[
        [
            "dt",
            "con_code",
            "snapshot_date",
            "weight",
            "observed",
            "suspended",
            "pct_chg",
            "vol",
            "amount",
        ]
    ].copy()
    output["dt"] = output["dt"].dt.strftime("%Y-%m-%d")
    output["snapshot_date"] = output["snapshot_date"].dt.strftime("%Y-%m-%d")
    output_path = artifacts / "constituent_daily_panel.csv.gz"
    output.to_csv(
        output_path,
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )

    storage_mb = output_path.stat().st_size / 1024 / 1024
    quality_gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "first_usable_session": membership["dt"].min().strftime("%Y-%m-%d"),
        "last_session": membership["dt"].max().strftime("%Y-%m-%d"),
        "sessions": int(membership["dt"].nunique()),
        "constituent_union": len(codes),
        "panel_rows": int(len(panel)),
        "member_count_min": int(session_counts.min()),
        "member_count_max": int(session_counts.max()),
        "daily_api_calls": daily_calls,
        "suspension_api_calls": suspension_calls,
        "daily_duplicate_rows": daily_duplicate_rows,
        "observed_rows": int(panel["observed"].sum()),
        "suspended_rows": int(panel["suspended"].sum()),
        "unexplained_missing_rows": int(len(unexplained)),
        "finite_observed_pct_change": finite_returns,
        "nonnegative_observed_volume_and_amount": nonnegative_activity,
        "causal_membership": causal_membership,
        "compressed_storage_mb": storage_mb,
        "expected_daily_calls": math.ceil(len(codes) / int(fetch["symbols_per_daily_request"])),
    }
    quality["passed"] = bool(
        quality["member_count_min"] == quality_gate["members_per_session"]
        and quality["member_count_max"] == quality_gate["members_per_session"]
        and daily_duplicate_rows == 0
        and unexplained.empty
        and finite_returns
        and nonnegative_activity
        and causal_membership
        and storage_mb <= quality_gate["compressed_storage_mb_max"]
    )
    _write_json(artifacts / "data_quality.json", quality)
    if not unexplained.empty:
        unresolved = unexplained.copy()
        unresolved["dt"] = unresolved["dt"].dt.strftime("%Y-%m-%d")
        unresolved.to_csv(artifacts / "unexplained_missing.csv", index=False, lineterminator="\n")

    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX30 执行\n\n"
        f"完整面板结果：`{status}`。{quality['sessions']}个可用交易日、"
        f"{quality['panel_rows']}条成员日记录、历史并集{quality['constituent_union']}只。"
        f"已观测{quality['observed_rows']}条，停牌解释{quality['suspended_rows']}条，"
        f"无法解释缺失{quality['unexplained_missing_rows']}条。\n\n"
        f"日线调用{quality['daily_api_calls']}次、停牌调用{quality['suspension_api_calls']}次，"
        f"压缩文件{quality['compressed_storage_mb']:.1f}MB。本轮没有聚合宽度或读取510500条件收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "完整历史时点成分日线面板可进入下一轮市场宽度特征定义。"
        if quality["passed"]
        else "完整面板未通过质量门，当前不得计算市场宽度或策略收益。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX30 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "breadth_feature_generation": False,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
