from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


EXPERIMENT_ID = "20260913_S005_EX45"


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    sys.path.insert(0, str(repo / "src"))
    sys.path.insert(0, str(repo / "packages" / "dataflows" / "src"))
    from czsc_trader.experiment_archive import (  # noqa: PLC0415
        build_experiment_manifest,
        validate_experiment_archive,
    )
    from dataflows.tushare_etf import fetch_etf_ohlcv  # noqa: PLC0415

    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("reads_post_signal_returns", "event_generation", "parameter_selection")
    ):
        raise ValueError("EX45 may only validate source data")

    start = str(protocol["development_start"])
    cutoff = str(protocol["development_cutoff"])
    source_quality: list[dict[str, object]] = []
    source_sessions: dict[str, set[pd.Timestamp]] = {}
    for position, source in enumerate(protocol["sources"], start=1):
        code = str(source["ts_code"])
        intraday, intraday_metadata = fetch_etf_ohlcv(
            code, start, cutoff, period=str(protocol["period"]), env_file=repo / ".env"
        )
        daily, daily_metadata = fetch_etf_ohlcv(
            code, start, cutoff, period="daily", env_file=repo / ".env"
        )
        intraday_dates = pd.DatetimeIndex(pd.to_datetime(intraday["Date"]).dt.normalize().unique())
        daily_dates = pd.DatetimeIndex(pd.to_datetime(daily["Date"]).dt.normalize().unique())
        duplicates = int(intraday.duplicated(["Date"], keep=False).sum())
        covered = daily_dates.intersection(intraday_dates)
        coverage = float(len(covered) / len(daily_dates))
        source_sessions[code] = set(intraday_dates)
        output = intraday.copy()
        output.to_csv(
            artifacts / f"{code.replace('.', '_')}_30m.csv.gz",
            index=False,
            compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
            lineterminator="\n",
        )
        source_quality.append(
            {
                "ts_code": code,
                "role": source["role"],
                "intraday_rows": int(len(intraday)),
                "intraday_sessions": int(len(intraday_dates)),
                "daily_sessions": int(len(daily_dates)),
                "daily_session_coverage": coverage,
                "duplicate_timestamps": duplicates,
                "first_session": intraday_dates.min().date().isoformat(),
                "last_session": intraday_dates.max().date().isoformat(),
                "intraday_adjustment_factor_sha256": intraday_metadata["adjustment_factor_sha256"],
                "daily_adjustment_factor_sha256": daily_metadata["adjustment_factor_sha256"],
                "same_adjustment_factors": intraday_metadata["adjustment_factor_sha256"]
                == daily_metadata["adjustment_factor_sha256"],
            }
        )
        print(f"sources {position}/{len(protocol['sources'])}: {code}", flush=True)

    common = set.intersection(*source_sessions.values())
    common_dates = pd.DatetimeIndex(sorted(common))
    gate = protocol["quality_gate"]
    passed = bool(
        len(common_dates) >= int(gate["minimum_common_sessions"])
        and common_dates.max() == pd.Timestamp(gate["required_end_date"])
        and all(
            item["daily_session_coverage"] >= float(gate["minimum_daily_session_coverage"])
            and item["duplicate_timestamps"] <= int(gate["maximum_duplicate_timestamps"])
            and item["same_adjustment_factors"]
            for item in source_quality
        )
    )
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "common_sessions": int(len(common_dates)),
        "common_first_session": common_dates.min().date().isoformat(),
        "common_last_session": common_dates.max().date().isoformat(),
        "source_quality": source_quality,
        "post_signal_returns_read": False,
        "passed": passed,
    }
    _write(artifacts / "data_quality.json", quality)
    pd.DataFrame({"dt": common_dates.strftime("%Y-%m-%d")}).to_csv(
        artifacts / "common_sessions.csv", index=False, lineterminator="\n"
    )

    status = "PASS" if passed else "FAIL"
    coverage_text = "；".join(
        f"{item['ts_code']} {item['intraday_sessions']}日/{item['daily_session_coverage']:.2%}"
        for item in source_quality
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX45 执行\n\n"
        f"数据门结果：`{status}`。共有交易日{len(common_dates)}个，范围"
        f"{quality['common_first_session']}至{quality['common_last_session']}。"
        f"各源30分钟覆盖：{coverage_text}。本轮没有生成信号或读取信号后收益。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_CROSS_ETF_TRANSMISSION_PREREGISTRATION" if passed else "STOP_CROSS_ETF_ROUTE_ON_DATA"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX45 结论\n\n"
        f"裁决：`{decision}`。没有创建候选、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": cutoff,
            "status": status,
            "decision": decision,
            "reads_prices_or_returns": False,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
