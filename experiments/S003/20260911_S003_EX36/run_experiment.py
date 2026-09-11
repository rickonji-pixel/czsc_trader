from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX36"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _record_access_failure(
    experiment: Path, artifacts: Path, dataset: dict[str, object], exc: Exception
) -> None:
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "passed": False,
        "access_available": False,
        "provider_error_type": type(exc).__name__,
        "provider_error": str(exc),
        "conditional_return_analysis": False,
    }
    _write_json(artifacts / "data_quality.json", quality)
    (experiment / "03_execution.md").write_text(
        "# S003 EX36 执行\n\n"
        "数据门结果：`FAIL`。当前Tushare账号无法读取510500融资融券明细，没有读取任何"
        "条件收益。供应商原始错误保存在机器证据中。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX36 结论\n\n"
        "结论：`FAIL`。当前数据权限不满足杠杆资金流研究的数据门，停止该信息源。"
        "本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": "FAIL",
            "failure_reason": "PROVIDER_ACCESS_UNAVAILABLE",
            "conditional_return_analysis": False,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "conditional_return_analysis",
        "signal_generation",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX36 may only validate ETF margin-flow data feasibility")

    dataset = protocol["dataset"]
    calendar_manifest = repo_root / dataset["calendar_manifest"]
    if _sha256(calendar_manifest) != dataset["calendar_manifest_sha256"]:
        raise ValueError("510500 calendar manifest differs from frozen evidence")
    daily = load_market_data(repo_root / "data" / "raw", "510500.SH").daily
    calendar = pd.DatetimeIndex(pd.to_datetime(daily["dt"]).dt.normalize())
    calendar = calendar[(calendar >= dataset["start"]) & (calendar <= dataset["development_cutoff"])]
    if calendar.has_duplicates or not calendar.is_monotonic_increasing:
        raise ValueError("510500 master calendar must be unique and ascending")

    source = protocol["source"]
    try:
        pro = get_tushare_pro(repo_root / ".env")
        frame = pro.margin_detail(
            ts_code=protocol["research_target"]["trade_symbol"],
            start_date=dataset["start"].replace("-", ""),
            end_date=dataset["development_cutoff"].replace("-", ""),
            fields=",".join(source["fields"]),
        )
    except Exception as exc:  # Tushare exposes provider failures as a generic exception.
        _record_access_failure(experiment, artifacts, dataset, exc)
        return
    if frame is None or frame.empty:
        _record_access_failure(
            experiment, artifacts, dataset, ValueError("Tushare returned no margin-detail data")
        )
        return

    frame = frame.copy()
    missing_fields = sorted(set(source["fields"]).difference(frame.columns))
    if missing_fields:
        raise ValueError(f"margin-detail data missing fields {missing_fields}")
    frame["dt"] = pd.to_datetime(frame["trade_date"].astype(str), format="%Y%m%d").dt.normalize()
    frame = frame.loc[(frame["dt"] >= calendar.min()) & (frame["dt"] <= calendar.max())]
    frame = frame.sort_values("dt").reset_index(drop=True)
    frame["available_for_strategy_from"] = frame["dt"].map(
        lambda value: calendar[calendar > value].min() if bool((calendar > value).any()) else pd.NaT
    )
    numeric = ["rzye", "rqye", "rzmre", "rqyl", "rzche", "rqchl", "rqmcl", "rzrqye"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame["rzye_identity_residual"] = (
        frame["rzye"] - frame["rzye"].shift(1) - frame["rzmre"] + frame["rzche"]
    )
    frame["rzye_identity_residual_ratio"] = frame["rzye_identity_residual"].abs().div(
        frame["rzye"].shift(1).replace(0, np.nan)
    )

    observed = pd.DatetimeIndex(frame["dt"])
    missing_dates = calendar.difference(observed)
    extra_dates = observed.difference(calendar)
    duplicate_rows = int(frame.duplicated("dt", keep=False).sum())
    coverage_ratio = float(len(calendar.intersection(observed.unique())) / len(calendar))
    core = list(protocol["quality_gate"]["finite_nonnegative_core_fields"])
    finite_nonnegative_core = bool(
        np.isfinite(frame[core].to_numpy(dtype=float)).all()
        and (frame[core] >= 0).all().all()
    )
    short_fields = ["rqye", "rqyl", "rqchl", "rqmcl"]
    quality_gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "access_available": True,
        "master_sessions": int(len(calendar)),
        "source_rows": int(len(frame)),
        "first_master_session": calendar.min().strftime("%Y-%m-%d"),
        "last_master_session": calendar.max().strftime("%Y-%m-%d"),
        "first_source_session": frame["dt"].min().strftime("%Y-%m-%d"),
        "last_source_session": frame["dt"].max().strftime("%Y-%m-%d"),
        "coverage_ratio": coverage_ratio,
        "missing_calendar_dates": [value.strftime("%Y-%m-%d") for value in missing_dates],
        "extra_source_dates": [value.strftime("%Y-%m-%d") for value in extra_dates],
        "duplicate_date_rows": duplicate_rows,
        "finite_nonnegative_core_fields": finite_nonnegative_core,
        "short_field_missing_ratio": {
            column: float(frame[column].isna().mean()) for column in short_fields
        },
        "median_financing_balance_identity_residual_ratio": float(
            frame["rzye_identity_residual_ratio"].median(skipna=True)
        ),
        "p95_financing_balance_identity_residual_ratio": float(
            frame["rzye_identity_residual_ratio"].quantile(0.95)
        ),
        "backfill_calls": 1,
        "daily_incremental_calls": 1,
        "documented_publication": source["documented_publication"],
        "earliest_strategy_use": source["earliest_strategy_use"],
        "forward_filled_rows": 0,
    }
    quality["passed"] = bool(
        duplicate_rows == 0
        and extra_dates.empty
        and calendar.min() in observed
        and calendar.max() in observed
        and coverage_ratio >= float(quality_gate["minimum_calendar_coverage_ratio"])
        and finite_nonnegative_core
        and quality["backfill_calls"] <= int(quality_gate["maximum_backfill_calls"])
        and quality["daily_incremental_calls"]
        <= int(quality_gate["maximum_daily_incremental_calls"])
    )

    output = frame.copy()
    for column in ("dt", "available_for_strategy_from"):
        output[column] = pd.to_datetime(output[column]).dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "etf_margin_flow_panel.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    _write_json(artifacts / "data_quality.json", quality)
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX36 执行\n\n"
        f"数据门结果：`{status}`。主日历{quality['master_sessions']}日，接口返回"
        f"{quality['source_rows']}日，覆盖率{quality['coverage_ratio']:.2%}，缺失"
        f"{len(missing_dates)}日，重复{duplicate_rows}行。融资余额恒等式残差比中位数"
        f"{quality['median_financing_balance_identity_residual_ratio']:.3%}、P95为"
        f"{quality['p95_financing_balance_identity_residual_ratio']:.3%}，后两项仅作诊断。\n\n"
        "本轮没有读取条件收益、生成信号或选择阈值。\n",
        encoding="utf-8",
    )
    conclusion = (
        "510500融资交易明细满足下一轮杠杆资金流机制研究的数据要求。"
        if quality["passed"]
        else "510500融资交易明细未通过冻结的数据门，当前不得进入收益研究。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX36 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有创建候选，也没有修改SM或PTE。\n",
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
            "conditional_return_analysis": False,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
