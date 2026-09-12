from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S004_EX28"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _fetch_moneyflow(
    pro: object, code: str, start: str, end: str, fields: list[str]
) -> pd.DataFrame:
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
        except Exception as exc:  # pragma: no cover - provider retry
            last_error = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"moneyflow failed for {code}") from last_error


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
        raise ValueError("EX28 may only validate constituent moneyflow data")

    dataset = protocol["dataset"]
    source = repo / "experiments" / "S004" / dataset["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source / "artifacts" / "constituent_daily_panel.csv.gz": dataset["source_panel_sha256"],
        source / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(source / "artifacts" / "data_quality.json")["passed"]:
        raise ValueError("EX25 source data gate did not pass")

    membership = pd.read_csv(source / "artifacts" / "constituent_daily_panel.csv.gz")
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

    pro = get_tushare_pro(repo / ".env")
    fields = list(protocol["source"]["fields"])
    collected: list[pd.DataFrame] = []
    calls = 0
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
        if calls % 50 == 0 or calls == len(membership_dates):
            print(f"moneyflow backfill {calls}/{len(membership_dates)}", flush=True)
        time.sleep(0.13)

    flow = (
        pd.concat(collected, ignore_index=True)
        if collected
        else pd.DataFrame(columns=[*fields, "dt"])
    )
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
    daily = merged.groupby("dt", sort=True, observed=True)["observed_moneyflow"].agg(
        ["sum", "count"]
    )
    daily["coverage_ratio"] = daily["sum"] / daily["count"]
    finite = bool(
        np.isfinite(
            merged.loc[merged["observed_moneyflow"], "net_mf_amount"].to_numpy(dtype=float)
        ).all()
    )
    first_date = scoped["dt"].min()
    last_date = scoped["dt"].max()
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "access_available": True,
        "expected_member_sessions": int(len(merged)),
        "observed_member_sessions": int(merged["observed_moneyflow"].sum()),
        "missing_member_sessions": int((~merged["observed_moneyflow"]).sum()),
        "overall_coverage_ratio": float(merged["observed_moneyflow"].mean()),
        "daily_coverage_p10": float(daily["coverage_ratio"].quantile(0.10)),
        "daily_coverage_median": float(daily["coverage_ratio"].median()),
        "daily_coverage_min": float(daily["coverage_ratio"].min()),
        "duplicate_member_date_rows": duplicate_rows,
        "first_session": first_date.strftime("%Y-%m-%d"),
        "last_session": last_date.strftime("%Y-%m-%d"),
        "first_session_covered": bool(daily.loc[first_date, "sum"] > 0),
        "last_session_covered": bool(daily.loc[last_date, "sum"] > 0),
        "finite_observed_net_mf_amount": finite,
        "backfill_calls": calls,
        "daily_incremental_calls": 1,
        "filled_missing_rows": 0,
        "earliest_strategy_use": protocol["source"]["earliest_strategy_use"],
        "conditional_return_analysis": False,
    }
    quality["passed"] = bool(
        duplicate_rows == 0
        and quality["overall_coverage_ratio"] >= gate["minimum_overall_coverage_ratio"]
        and quality["daily_coverage_p10"] >= gate["minimum_daily_coverage_p10"]
        and quality["first_session_covered"]
        and quality["last_session_covered"]
        and finite
        and calls <= gate["maximum_backfill_calls"]
        and quality["daily_incremental_calls"] <= gate["maximum_daily_incremental_calls"]
    )

    output = merged.drop(columns="ts_code").copy()
    output["dt"] = output["dt"].dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "constituent_moneyflow_panel.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    daily.reset_index().assign(dt=lambda value: value["dt"].dt.strftime("%Y-%m-%d")).to_csv(
        artifacts / "daily_coverage.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    _write_json(artifacts / "data_quality.json", quality)
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S004 EX28 执行\n\n"
        f"资金流数据门：`{status}`。历史正常交易成员日{quality['expected_member_sessions']:,}条，"
        f"覆盖{quality['observed_member_sessions']:,}条，总覆盖率"
        f"{quality['overall_coverage_ratio']:.2%}；单日覆盖率中位数"
        f"{quality['daily_coverage_median']:.2%}、P10 {quality['daily_coverage_p10']:.2%}。"
        f"首次回填调用{calls}次，缺失未填充。\n\n本轮没有生成事件或读取收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "历史时点成分资金流面板可进入吸筹背离事件定义。"
        if quality["passed"]
        else "历史时点成分资金流未通过数据门，停止该信息源。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX28 结论\n\n"
        f"结论：`{status}`。{conclusion}没有创建候选或修改SM/PTE。\n",
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
            "conditional_return_analysis": False,
            "event_generation": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
