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


EXPERIMENT_ID = "20260913_S004_EX25"


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
        if sessions.empty:
            continue
        members = snapshots.loc[
            snapshots["trade_date"].eq(snapshot_date), ["con_code", "weight"]
        ].copy()
        repeated = members.loc[members.index.repeat(len(sessions))].reset_index(drop=True)
        repeated["dt"] = np.tile(sessions.to_numpy(), len(members))
        repeated["snapshot_date"] = snapshot_date
        frames.append(repeated)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["dt", "con_code"]).reset_index(drop=True)


def _fetch_batched(
    method,
    codes: list[str],
    batch_size: int,
    **kwargs,
) -> tuple[pd.DataFrame, int]:
    frames: list[pd.DataFrame] = []
    batches = _chunks(codes, batch_size)
    for index, batch in enumerate(batches, start=1):
        frame = _request_with_retry(method, ts_code=",".join(batch), **kwargs)
        if not frame.empty:
            frames.append(frame)
        if index % 25 == 0 or index == len(batches):
            print(f"batches {index}/{len(batches)}", flush=True)
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(), len(batches))


def _fetch_lifecycle(pro, codes: list[str]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for code in codes:
        for status in ("D", "P", "L"):
            frame = _request_with_retry(
                pro.stock_basic,
                ts_code=code,
                list_status=status,
                fields="ts_code,name,list_status,list_date,delist_date",
            )
            if not frame.empty:
                frames.append(frame)
                break
    if not frames:
        return pd.DataFrame(columns=["ts_code", "name", "list_status", "list_date", "delist_date"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "conditional_return_analysis",
        "breadth_feature_generation",
        "signal_generation",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX25 may only construct the constituent daily panel")

    source_spec = protocol["source"]
    source = repo / "experiments" / "S004" / source_spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["experiment_manifest_sha256"],
        source / "artifacts" / "data_quality.json": source_spec["data_quality_sha256"],
        source / "artifacts" / "index_weight_snapshots.csv.gz": source_spec[
            "membership_file_sha256"
        ],
        repo / source_spec["calendar_manifest"]: source_spec["calendar_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(source / "artifacts" / "data_quality.json")["passed"]:
        raise ValueError("EX24 data gate did not pass")

    dataset = protocol["dataset"]
    start = pd.Timestamp(dataset["start"])
    cutoff = pd.Timestamp(dataset["development_cutoff"])
    master = load_market_data(repo / "data" / "raw", "588080.SH").daily
    calendar = pd.DatetimeIndex(pd.to_datetime(master["dt"]).dt.normalize())
    calendar = calendar[(calendar >= start) & (calendar <= cutoff)]
    snapshots = pd.read_csv(source / "artifacts" / "index_weight_snapshots.csv.gz")
    snapshots["trade_date"] = pd.to_datetime(snapshots["trade_date"])
    membership = _build_membership_panel(snapshots, calendar)
    if membership.empty:
        raise ValueError("point-in-time membership panel is empty")

    fetch = protocol["fetch"]
    codes = sorted(membership["con_code"].unique())
    pro = get_tushare_pro(repo / ".env")
    start_api = start.strftime("%Y%m%d")
    end_api = cutoff.strftime("%Y%m%d")
    daily, daily_calls = _fetch_batched(
        pro.daily,
        codes,
        int(fetch["symbols_per_daily_request"]),
        start_date=start_api,
        end_date=end_api,
        fields=",".join(fetch["daily_fields"]),
    )
    if daily.empty:
        raise ValueError("Tushare returned no constituent daily rows")
    daily["trade_date"] = pd.to_datetime(
        daily["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    duplicate_daily = int(daily.duplicated(["trade_date", "ts_code"], keep=False).sum())
    daily = daily.drop_duplicates(["trade_date", "ts_code"])
    panel = membership.merge(
        daily.rename(columns={"trade_date": "dt", "ts_code": "con_code"}),
        on=["dt", "con_code"],
        how="left",
        validate="one_to_one",
    )
    panel["observed"] = panel["close"].notna()

    missing_codes = sorted(panel.loc[~panel["observed"], "con_code"].unique())
    suspensions, suspension_calls = _fetch_batched(
        pro.suspend_d,
        missing_codes,
        int(fetch["symbols_per_suspension_request"]),
        start_date=start_api,
        end_date=end_api,
        suspend_type="S",
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
        not observed and (dt, code) in suspension_keys
        for dt, code, observed in zip(panel["dt"], panel["con_code"], panel["observed"], strict=False)
    ]

    unresolved_mask = ~panel["observed"] & ~panel["suspended"]
    lifecycle = _fetch_lifecycle(pro, sorted(panel.loc[unresolved_mask, "con_code"].unique()))
    if not lifecycle.empty:
        lifecycle["list_date"] = pd.to_datetime(
            lifecycle["list_date"].astype(str), format="%Y%m%d", errors="coerce"
        )
        lifecycle["delist_date"] = pd.to_datetime(
            lifecycle["delist_date"].astype(str), format="%Y%m%d", errors="coerce"
        )
    delist_dates = lifecycle.set_index("ts_code")["delist_date"].to_dict()
    stale = pd.Series(False, index=panel.index)
    for code, delist_date in delist_dates.items():
        if pd.notna(delist_date):
            stale |= panel["con_code"].eq(code) & unresolved_mask & panel["dt"].ge(delist_date)

    panel["record_status"] = "OBSERVED"
    panel.loc[~panel["observed"] & panel["suspended"], "record_status"] = "SUSPENDED"
    panel.loc[stale, "record_status"] = "DELISTED_STALE_MEMBERSHIP"
    remaining = panel.loc[unresolved_mask & ~stale, ["dt", "con_code"]]
    unexpected = sorted(set(panel["record_status"]) - set(protocol["allowed_record_statuses"]))
    observed = panel.loc[panel["observed"]]
    session_counts = membership.groupby("dt")["con_code"].nunique()
    causal = bool((membership["snapshot_date"] < membership["dt"]).all())
    no_filled_missing = int(
        panel.loc[~panel["observed"], ["pct_chg", "vol", "amount"]].notna().any(axis=1).sum()
    )

    output = panel[
        [
            "dt",
            "con_code",
            "snapshot_date",
            "weight",
            "record_status",
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
    lifecycle_out = lifecycle.copy()
    for column in ("list_date", "delist_date"):
        if column in lifecycle_out:
            lifecycle_out[column] = pd.to_datetime(
                lifecycle_out[column], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
    lifecycle_out.to_csv(artifacts / "lifecycle_evidence.csv", index=False, lineterminator="\n")
    if not remaining.empty:
        remaining_out = remaining.copy()
        remaining_out["dt"] = remaining_out["dt"].dt.strftime("%Y-%m-%d")
        remaining_out.to_csv(
            artifacts / "unexplained_missing.csv", index=False, lineterminator="\n"
        )

    storage_mb = output_path.stat().st_size / 1024 / 1024
    gate = protocol["quality_gate"]
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
        "expected_daily_calls": math.ceil(len(codes) / int(fetch["symbols_per_daily_request"])),
        "suspension_api_calls": suspension_calls,
        "lifecycle_api_codes": int(len(lifecycle)),
        "daily_duplicate_rows": duplicate_daily,
        "record_status_counts": {
            key: int(value) for key, value in panel["record_status"].value_counts().items()
        },
        "remaining_unexplained_rows": int(len(remaining)),
        "unexpected_statuses": unexpected,
        "finite_observed_pct_change": bool(
            np.isfinite(observed["pct_chg"].to_numpy(dtype=float)).all()
        ),
        "nonnegative_observed_volume_and_amount": bool(
            (observed[["vol", "amount"]] >= 0).all().all()
        ),
        "causal_membership": causal,
        "price_or_return_fill_rows": no_filled_missing,
        "compressed_storage_mb": storage_mb,
    }
    quality["passed"] = bool(
        quality["member_count_min"] == gate["members_per_session"]
        and quality["member_count_max"] == gate["members_per_session"]
        and quality["daily_duplicate_rows"] == 0
        and quality["remaining_unexplained_rows"] == 0
        and not unexpected
        and quality["finite_observed_pct_change"]
        and quality["nonnegative_observed_volume_and_amount"]
        and quality["causal_membership"]
        and quality["price_or_return_fill_rows"] == 0
        and quality["compressed_storage_mb"] <= gate["compressed_storage_mb_max"]
    )
    _write_json(artifacts / "data_quality.json", quality)

    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S004 EX25 执行\n\n"
        f"完整面板：`{status}`。{quality['sessions']}个交易日、{quality['panel_rows']}条成员日记录、"
        f"历史并集{quality['constituent_union']}只；状态分布{quality['record_status_counts']}，"
        f"无法解释缺失{quality['remaining_unexplained_rows']}条。\n\n"
        f"日线调用{quality['daily_api_calls']}次、停牌调用{quality['suspension_api_calls']}次，"
        f"压缩文件{quality['compressed_storage_mb']:.1f}MB。本轮没有聚合宽度或读取条件收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "生命周期感知的历史时点成分面板可以进入宽度特征定义。"
        if quality["passed"]
        else "完整面板未通过质量门，当前不得计算宽度或策略收益。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX25 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
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
