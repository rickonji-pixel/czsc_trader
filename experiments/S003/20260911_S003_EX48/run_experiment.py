from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.bar_utils import validate_a_share_intraday_bars, validate_intraday_against_daily
from dataflows.tushare_etf import fetch_etf_ohlcv


EXPERIMENT_ID = "20260911_S003_EX48"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _csv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.rename(
        columns={
            "Date": "datetime",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
            "Amount": "amount",
        }
    ).copy()
    result["datetime"] = pd.to_datetime(result["datetime"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    return result


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "event_generation",
        "conditional_return_analysis",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX48 may only validate intraday execution data")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    minute, minute_metadata = fetch_etf_ohlcv(
        str(target["symbol"]),
        str(dataset["start"]),
        str(dataset["development_cutoff"]),
        "5m",
        env_file=repo / ".env",
    )
    daily, daily_metadata = fetch_etf_ohlcv(
        str(target["symbol"]),
        str(dataset["start"]),
        str(dataset["development_cutoff"]),
        "daily",
        env_file=repo / ".env",
    )
    minute = minute.sort_values("Date").reset_index(drop=True)
    daily = daily.sort_values("Date").reset_index(drop=True)
    session = validate_a_share_intraday_bars(minute, "5m", require_complete_days=True)
    reconciliation = validate_intraday_against_daily(minute, daily, "5m")
    timestamps = pd.to_datetime(minute["Date"])
    session_dates = timestamps.dt.normalize()
    first = session_dates.min()
    last = session_dates.max()
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "access_available": True,
        "bar_count": int(len(minute)),
        "complete_session_count": int(session["complete_day_count"]),
        "expected_bars_per_session": int(session["expected_bars_per_day"]),
        "daily_matched_sessions": int(reconciliation["matched_day_count"]),
        "first_session": first.strftime("%Y-%m-%d"),
        "last_session": last.strftime("%Y-%m-%d"),
        "timestamps_unique": bool(not timestamps.duplicated().any()),
        "timestamps_monotonic": bool(timestamps.is_monotonic_increasing),
        "adjustment_factor_consistent": bool(
            minute_metadata["adjustment_factor_sha256"]
            == daily_metadata["adjustment_factor_sha256"]
        ),
        "conditional_return_analysis": False,
    }
    quality["passed"] = bool(
        quality["complete_session_count"] >= int(gate["minimum_complete_sessions"])
        and quality["expected_bars_per_session"]
        == int(gate["expected_bars_per_complete_session"])
        and quality["daily_matched_sessions"] == quality["complete_session_count"]
        and quality["first_session"] >= str(dataset["start"])
        and quality["last_session"] == str(dataset["development_cutoff"])
        and quality["timestamps_unique"]
        and quality["timestamps_monotonic"]
        and quality["adjustment_factor_consistent"]
    )

    _csv_frame(minute).to_csv(
        artifacts / "512100_5m.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    _csv_frame(daily).to_csv(
        artifacts / "512100_daily.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    _write_json(artifacts / "fetch_metadata.json", {"minute": minute_metadata, "daily": daily_metadata})
    _write_json(artifacts / "data_quality.json", quality)
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX48 执行\n\n"
        f"数据门：`{status}`。覆盖{quality['first_session']}至{quality['last_session']}，"
        f"{quality['complete_session_count']}个完整交易日、{quality['bar_count']:,}根5分钟柱；"
        f"日线对账{quality['daily_matched_sessions']}日。\n\n"
        "本轮没有生成事件或读取条件收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "512100执行行情满足独立横截面复现要求。"
        if quality["passed"]
        else "512100执行行情未通过冻结数据门，横截面复现停止。"
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S003 EX48 结论\n\n结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": target["symbol"],
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
