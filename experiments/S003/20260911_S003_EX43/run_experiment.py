from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX43"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _fetch_moneyflow(pro: object, code: str, start: str, end: str, fields: list[str]) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            frame = pro.moneyflow(
                ts_code=code,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
                fields=",".join(fields),
            )
            return pd.DataFrame() if frame is None else frame
        except Exception as exc:  # Tushare exposes provider failures as a generic exception.
            last_error = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"moneyflow failed for {code}") from last_error


def _record_access_failure(
    experiment: Path,
    artifacts: Path,
    dataset: dict[str, object],
    calls: int,
    exc: Exception,
) -> None:
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "passed": False,
        "access_available": False,
        "completed_backfill_calls": calls,
        "provider_error_type": type(exc).__name__,
        "provider_error": str(exc),
        "conditional_return_analysis": False,
    }
    _write_json(artifacts / "data_quality.json", quality)
    (experiment / "03_execution.md").write_text(
        "# S003 EX43 执行\n\n"
        f"数据门：`FAIL`。完成{calls}次回补调用后供应商访问失败；本轮没有读取条件收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX43 结论\n\n"
        "结论：`FAIL`。当前数据访问不能形成受控的历史成分资金流面板，停止该信息源。"
        "本轮没有创建候选或修改SM/PTE。\n",
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
        "event_generation",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX43 may only validate constituent moneyflow data")

    dataset = protocol["dataset"]
    source_experiment = repo_root / "experiments" / dataset["source_experiment"]
    source_paths = {
        source_experiment / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source_experiment / "artifacts" / "lifecycle_aware_constituent_panel.csv.gz": dataset[
            "source_panel_sha256"
        ],
        source_experiment / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
    }
    for path, expected in source_paths.items():
        if _sha256(path) != expected:
            raise ValueError(f"source digest differs: {path}")
    if not _read_json(source_experiment / "artifacts" / "data_quality.json").get("passed"):
        raise ValueError("source constituent panel did not pass its data gate")

    membership = pd.read_csv(
        source_experiment / "artifacts" / "lifecycle_aware_constituent_panel.csv.gz"
    )
    membership["dt"] = pd.to_datetime(membership["dt"]).dt.normalize()
    scoped = membership.loc[
        membership["dt"].between(dataset["start"], dataset["development_cutoff"])
        & membership["record_status"].eq(protocol["membership"]["required_record_status"])
        & pd.to_numeric(membership["vol"], errors="coerce").gt(0),
        ["dt", "con_code", "snapshot_date", "weight"],
    ].copy()
    if scoped.duplicated(["dt", "con_code"]).any():
        raise ValueError("source membership contains duplicate member dates")
    membership_dates = {
        code: set(rows["dt"])
        for code, rows in scoped.groupby("con_code", sort=True, observed=True)
    }

    pro = get_tushare_pro(repo_root / ".env")
    fields = list(protocol["source"]["fields"])
    collected: list[pd.DataFrame] = []
    calls = 0
    try:
        for code, allowed_dates in membership_dates.items():
            raw = _fetch_moneyflow(pro, code, dataset["start"], dataset["development_cutoff"], fields)
            calls += 1
            if not raw.empty:
                missing = sorted(set(fields).difference(raw.columns))
                if missing:
                    raise ValueError(f"moneyflow {code} missing fields {missing}")
                raw = raw.copy()
                raw["dt"] = pd.to_datetime(
                    raw["trade_date"].astype(str), format="%Y%m%d"
                ).dt.normalize()
                collected.append(raw.loc[raw["dt"].isin(allowed_dates), [*fields, "dt"]])
            if calls % 100 == 0:
                print(f"moneyflow backfill {calls}/{len(membership_dates)}", flush=True)
            time.sleep(0.13)
    except Exception as exc:
        _record_access_failure(experiment, artifacts, dataset, calls, exc)
        return

    flow = pd.concat(collected, ignore_index=True) if collected else pd.DataFrame(columns=[*fields, "dt"])
    flow["net_mf_amount"] = pd.to_numeric(flow["net_mf_amount"], errors="coerce")
    duplicate_rows = int(flow.duplicated(["dt", "ts_code"], keep=False).sum())
    merged = scoped.merge(
        flow[["dt", "ts_code", "net_mf_amount"]],
        left_on=["dt", "con_code"],
        right_on=["dt", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    merged["observed_moneyflow"] = merged["net_mf_amount"].notna()
    daily = merged.groupby("dt", sort=True, observed=True)["observed_moneyflow"].agg(["sum", "count"])
    daily["coverage_ratio"] = daily["sum"] / daily["count"]
    overall_coverage = float(merged["observed_moneyflow"].mean())
    daily_p10 = float(daily["coverage_ratio"].quantile(0.10))
    first_date = scoped["dt"].min()
    last_date = scoped["dt"].max()
    finite_observed = bool(
        np.isfinite(merged.loc[merged["observed_moneyflow"], "net_mf_amount"].to_numpy()).all()
    )
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "access_available": True,
        "expected_member_sessions": int(len(merged)),
        "observed_member_sessions": int(merged["observed_moneyflow"].sum()),
        "missing_member_sessions": int((~merged["observed_moneyflow"]).sum()),
        "overall_coverage_ratio": overall_coverage,
        "daily_coverage_p10": daily_p10,
        "daily_coverage_median": float(daily["coverage_ratio"].median()),
        "daily_coverage_min": float(daily["coverage_ratio"].min()),
        "duplicate_member_date_rows": duplicate_rows,
        "first_session": first_date.strftime("%Y-%m-%d"),
        "last_session": last_date.strftime("%Y-%m-%d"),
        "first_session_covered": bool(daily.loc[first_date, "sum"] > 0),
        "last_session_covered": bool(daily.loc[last_date, "sum"] > 0),
        "finite_observed_net_mf_amount": finite_observed,
        "backfill_calls": calls,
        "daily_incremental_calls": 1,
        "filled_missing_rows": 0,
        "earliest_strategy_use": protocol["source"]["earliest_strategy_use"],
        "conditional_return_analysis": False,
    }
    quality["passed"] = bool(
        duplicate_rows == 0
        and overall_coverage >= float(gate["minimum_overall_coverage_ratio"])
        and daily_p10 >= float(gate["minimum_daily_coverage_p10"])
        and quality["first_session_covered"]
        and quality["last_session_covered"]
        and finite_observed
        and calls <= int(gate["maximum_backfill_calls"])
        and quality["daily_incremental_calls"] <= int(gate["maximum_daily_incremental_calls"])
    )

    output = merged.copy()
    output["dt"] = output["dt"].dt.strftime("%Y-%m-%d")
    output = output.drop(columns="ts_code")
    output.to_csv(
        artifacts / "constituent_moneyflow_panel.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    daily.reset_index().assign(dt=lambda value: value["dt"].dt.strftime("%Y-%m-%d")).to_csv(
        artifacts / "daily_coverage.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    _write_json(artifacts / "data_quality.json", quality)
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX43 执行\n\n"
        f"数据门：`{status}`。历史正常交易成员日期{quality['expected_member_sessions']:,}条，"
        f"资金流覆盖{quality['observed_member_sessions']:,}条，总体覆盖率"
        f"{quality['overall_coverage_ratio']:.2%}；单日覆盖率中位数"
        f"{quality['daily_coverage_median']:.2%}、第10百分位{quality['daily_coverage_p10']:.2%}。"
        f"共调用{calls}次历史回补，缺失未填充。\n\n本轮没有生成事件或读取条件收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "历史成分资金流面板满足下一轮事件普查的数据要求。"
        if quality["passed"]
        else "历史成分资金流面板未通过冻结的数据门，停止该信息源。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX43 结论\n\n"
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
            "conditional_return_analysis": False,
            "event_generation": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
