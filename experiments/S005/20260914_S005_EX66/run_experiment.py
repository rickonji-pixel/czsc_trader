from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260914_S005_EX66"
REPORT_FIELDS = (
    "ts_code,name,report_date,report_title,report_type,classify,org_name,"
    "author_name,quarter,op_rt,op_pr,tp,np,eps,pe,rd,roe,ev_ebitda,rating,"
    "max_price,min_price"
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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_target_forward_returns"):
        raise ValueError("EX66 may not read target forward returns")

    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["constituent_experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["constituent_manifest_sha256"],
        source / "artifacts/constituent_panel.csv.gz": source_spec["constituent_panel_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    constituent = pd.read_csv(
        source / "artifacts/constituent_panel.csv.gz",
        usecols=["con_code", "dt", "weight"],
        parse_dates=["dt"],
    )
    constituent["dt"] = constituent["dt"].dt.normalize()
    calendar = pd.DatetimeIndex(sorted(constituent["dt"].unique()))
    development_start = pd.Timestamp(str(protocol["development_start"]))
    start = pd.Timestamp(str(protocol["constituent_coverage_start"]))
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    calendar = calendar[(calendar >= start) & (calendar <= cutoff)]
    if calendar.empty or calendar.min() != start or calendar.max() != cutoff:
        raise ValueError("constituent calendar differs from frozen development window")
    active = constituent.loc[
        constituent["dt"].isin(calendar), ["dt", "con_code", "weight"]
    ].drop_duplicates(["dt", "con_code"])
    codes = sorted(active["con_code"].astype(str).unique())

    pro = get_tushare_pro(repo / ".env")
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, str]] = []
    for position, code in enumerate(codes, start=1):
        try:
            frame = _request(
                pro.report_rc,
                ts_code=code,
                start_date=start.strftime("%Y%m%d"),
                end_date=cutoff.strftime("%Y%m%d"),
                fields=REPORT_FIELDS,
            )
            if not frame.empty:
                frames.append(frame)
        except Exception as error:  # preserve explicit per-member failure
            failures.append({"ts_code": code, "error": f"{type(error).__name__}: {error}"})
        if position % 25 == 0 or position == len(codes):
            print(f"report_rc {position}/{len(codes)}", flush=True)
    api_calls = len(codes)
    if not frames:
        raise ValueError("Tushare returned no sell-side forecasts")

    reports = pd.concat(frames, ignore_index=True)
    reports["report_date"] = pd.to_datetime(
        reports["report_date"], format="%Y%m%d", errors="coerce"
    ).dt.normalize()
    invalid_report_dates = int(reports["report_date"].isna().sum())
    reports = reports.loc[reports["report_date"].notna()].copy()
    reports = reports.loc[
        reports["report_date"].between(start, cutoff, inclusive="both")
    ].copy()
    numeric = ["op_rt", "op_pr", "tp", "np", "eps", "pe", "rd", "roe", "ev_ebitda", "max_price", "min_price"]
    for column in numeric:
        reports[column] = pd.to_numeric(reports[column], errors="coerce")

    exact_key = [column for column in REPORT_FIELDS.split(",") if column in reports.columns]
    exact_duplicate_rows = int(reports.duplicated(exact_key, keep=False).sum())
    reports = reports.drop_duplicates(exact_key).reset_index(drop=True)

    positions = calendar.searchsorted(reports["report_date"], side="right")
    valid_position = positions < len(calendar)
    reports = reports.loc[valid_position].copy()
    reports["available_session"] = calendar.take(positions[valid_position])
    reports = reports.merge(
        active.rename(columns={"dt": "available_session", "con_code": "ts_code"}),
        on=["available_session", "ts_code"],
        how="left",
        validate="many_to_one",
    )
    reports["active_member_on_availability"] = reports["weight"].notna()
    active_reports = reports.loc[reports["active_member_on_availability"]].copy()
    active_reports["numeric_forecast_available"] = active_reports[["np", "eps"]].notna().any(axis=1)
    yearly = active_reports.groupby(active_reports["report_date"].dt.year).size()

    gate = protocol["quality_gate"]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "calendar_sessions": int(len(calendar)),
        "development_start": development_start.strftime("%Y-%m-%d"),
        "constituent_coverage_start": start.strftime("%Y-%m-%d"),
        "uncovered_initial_sessions": int(protocol["uncovered_initial_sessions"]),
        "historical_member_union": int(len(codes)),
        "api_calls": api_calls,
        "api_successes": int(len(codes) - len(failures)),
        "api_success_ratio": float((len(codes) - len(failures)) / len(codes)),
        "raw_report_rows": int(len(reports)),
        "active_report_rows": int(len(active_reports)),
        "members_with_active_reports": int(active_reports["ts_code"].nunique()),
        "unique_active_report_dates": int(active_reports["report_date"].nunique()),
        "numeric_forecast_coverage": float(active_reports["numeric_forecast_available"].mean()),
        "minimum_active_rows_per_year": int(yearly.min()) if not yearly.empty else 0,
        "active_rows_by_year": {str(int(year)): int(count) for year, count in yearly.items()},
        "invalid_report_dates": invalid_report_dates,
        "exact_duplicate_rows": exact_duplicate_rows,
        "causal_use": str(protocol["availability"]["conservative_use"]),
        "reads_target_forward_returns": False,
    }
    evidence["passed"] = bool(
        evidence["api_success_ratio"] >= float(gate["required_api_success_ratio"])
        and evidence["members_with_active_reports"] >= int(gate["minimum_members_with_active_reports"])
        and evidence["unique_active_report_dates"] >= int(gate["minimum_unique_report_dates"])
        and evidence["active_report_rows"] >= int(gate["minimum_active_report_rows"])
        and evidence["minimum_active_rows_per_year"] >= int(gate["minimum_active_rows_per_year"])
        and evidence["numeric_forecast_coverage"] >= float(gate["minimum_numeric_forecast_coverage"])
        and exact_duplicate_rows <= int(gate["maximum_exact_duplicate_rows"])
        and api_calls <= int(gate["maximum_api_calls"])
        and invalid_report_dates == 0
    )

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    active_reports.to_csv(
        artifacts / "active_constituent_forecasts.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
        date_format="%Y-%m-%d",
    )
    pd.DataFrame(failures, columns=["ts_code", "error"]).to_csv(
        artifacts / "api_failures.csv", index=False, lineterminator="\n"
    )
    _write(artifacts / "data_gate_evidence.json", evidence)

    status = "PASS" if evidence["passed"] else "FAIL"
    decision = (
        "PROCEED_TO_EXPECTATION_REVISION_MECHANISM_PREREGISTRATION"
        if evidence["passed"]
        else "STOP_EXPECTATION_REVISION_ROUTE_ON_DATA"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX66 执行\n\n"
        f"状态：`{status}`。{evidence['api_successes']}/{evidence['historical_member_union']}只历史成分请求成功，"
        f"形成{evidence['active_report_rows']}条历史时点有效预测、覆盖{evidence['members_with_active_reports']}只公司和"
        f"{evidence['unique_active_report_dates']}个研报日期。净利润或EPS预测覆盖"
        f"{evidence['numeric_forecast_coverage']:.2%}。\n\n"
        "本轮没有计算预期修订、生成事件或读取588080后续收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX66 结论\n\n"
        f"裁决：`{decision}`。卖方预测按研报日期后的下一588080交易日才可使用，并按该日历史成分过滤。"
        "通过只代表数据与因果契约可用；下一轮仍须先登记预期上修、预期拥挤等竞争解释。"
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
