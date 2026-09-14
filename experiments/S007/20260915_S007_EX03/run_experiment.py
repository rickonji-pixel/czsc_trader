from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import tushare as ts

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX03"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _a_share_sessions(repo: Path, start: str, end: str) -> pd.DatetimeIndex:
    rows = []
    for year in range(2021, 2027):
        rows.append(pd.read_csv(repo / f"data/raw/588080_daily_{year}.csv", usecols=["date"]))
    dates = pd.to_datetime(pd.concat(rows, ignore_index=True)["date"])
    selected = dates.loc[dates.between(pd.Timestamp(start), pd.Timestamp(end))]
    if selected.duplicated().any() or not selected.is_monotonic_increasing:
        raise ValueError("A-share session calendar must be unique and increasing")
    return pd.DatetimeIndex(selected, name="a_share_session")


def _coverage(
    sessions: pd.DatetimeIndex,
    source_dates: pd.Series,
    *,
    source_id: str,
) -> dict[str, object]:
    source = pd.DataFrame({"source_date": pd.to_datetime(source_dates).drop_duplicates().sort_values()})
    target = pd.DataFrame({"a_share_session": sessions})
    mapped = pd.merge_asof(
        target.sort_values("a_share_session"),
        source.sort_values("source_date"),
        left_on="a_share_session",
        right_on="source_date",
        direction="backward",
    )
    lag = (mapped["a_share_session"] - mapped["source_date"]).dt.days
    return {
        "source_id": source_id,
        "source_rows": int(len(source)),
        "mapped_sessions": int(mapped["source_date"].notna().sum()),
        "coverage": float(mapped["source_date"].notna().mean()),
        "future_source_dates": int((lag < 0).sum()),
        "maximum_calendar_lag_days": int(lag.max()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("reads_new_returns", "candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("data gate cannot read returns, select or deploy a candidate")

    start = str(protocol["development_start"]).replace("-", "")
    end = str(protocol["development_cutoff"]).replace("-", "")
    client = ts.pro_api()
    shibor = client.shibor(start_date=start, end_date=end)
    if shibor.empty:
        raise RuntimeError("Tushare shibor returned no rows")

    index_rows = []
    for code in protocol["apis"][1]["ts_codes"]:
        daily = client.index_daily(ts_code=code, start_date=start, end_date=end)
        basic = client.index_dailybasic(ts_code=code, start_date=start, end_date=end)
        if daily.empty or basic.empty:
            raise RuntimeError(f"Tushare index data returned no rows: {code}")
        merged = daily.merge(
            basic,
            on=["ts_code", "trade_date"],
            how="left",
            validate="one_to_one",
            suffixes=("", "_basic"),
        )
        index_rows.append(merged)
    indices = pd.concat(index_rows, ignore_index=True)

    shibor = shibor.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    indices = indices.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    shibor.to_csv(artifacts / "shibor.csv.gz", index=False, compression="gzip")
    indices.to_csv(artifacts / "broad_risk_indices.csv.gz", index=False, compression="gzip")

    sessions = _a_share_sessions(
        repo,
        str(protocol["development_start"]),
        str(protocol["development_cutoff"]),
    )
    audits = [_coverage(sessions, shibor["date"], source_id="SHIBOR")]
    for code, frame in indices.groupby("ts_code", sort=True):
        audits.append(_coverage(sessions, frame["trade_date"], source_id=str(code)))
    gate = protocol["coverage_gate"]
    pass_checks = [
        item["coverage"] >= float(gate["minimum_session_coverage"])
        and item["future_source_dates"] == 0
        and item["maximum_calendar_lag_days"] <= int(gate["maximum_calendar_lag_days"])
        for item in audits
    ]
    status = "PASS" if all(pass_checks) else "FAIL"
    decision = "PROCEED_TO_HYPOTHESIS_PREREGISTRATION" if status == "PASS" else "STOP_RISK_APPETITE_DATA_ROUTE"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": status,
        "decision": decision,
        "audits": audits,
        "shibor_rows": int(len(shibor)),
        "index_rows": int(len(indices)),
        "index_codes": sorted(indices["ts_code"].unique().tolist()),
        "artifacts": {
            "shibor": {"path": "artifacts/shibor.csv.gz", "sha256": _sha256(artifacts / "shibor.csv.gz")},
            "indices": {"path": "artifacts/broad_risk_indices.csv.gz", "sha256": _sha256(artifacts / "broad_risk_indices.csv.gz")},
        },
        "new_return_paths_read": 0,
        "input_selected": False,
        "template_selected": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "data_gate_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX03 执行记录\n\n"
        f"Tushare三个接口均成功返回并保存受管数据。SHIBOR共{len(shibor)}行，三个宽基指数"
        f"合计{len(indices)}行。全部覆盖和因果门结果为`{status}`。没有读取588080后续收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX03 结论\n\n"
        f"裁决：`{decision}`。国内利率和全市场风险偏好数据门{status}；"
        "该结论只授予H01机制预注册资格，不代表数据具有预测力。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE" if status == "PASS" else "FAILED",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)
    if status != "PASS":
        raise RuntimeError("risk appetite data gate failed")


if __name__ == "__main__":
    main()
