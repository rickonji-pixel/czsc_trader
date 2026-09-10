from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.cross_asset_daily_data import (
    DailySnapshot,
    canonicalize_daily,
    synchronize_snapshots,
)
from czsc_trader.data import load_execution_prices, load_market_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from dataflows.tushare_etf import fetch_etf_ohlcv, fetch_etf_unadjusted_daily


EXPERIMENT_ID = "20260911_S003_EX20"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    output = frame.copy()
    if "dt" in output:
        output["dt"] = pd.to_datetime(output["dt"]).dt.strftime("%Y-%m-%d")
    output.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def _assert_hash(path: Path, expected: str) -> None:
    if _sha256(path) != expected:
        raise ValueError(f"source hash differs: {path}")


def _load_510500(repo_root: Path, protocol: dict[str, object]) -> DailySnapshot:
    source = protocol["sources"]["510500.SH"]
    adjusted_manifest = repo_root / source["adjusted_manifest"]
    execution_manifest = repo_root / source["execution_manifest"]
    _assert_hash(adjusted_manifest, source["adjusted_manifest_sha256"])
    _assert_hash(execution_manifest, source["execution_manifest_sha256"])
    data = load_market_data(repo_root / "data" / "raw", "510500.SH")
    execution = load_execution_prices(repo_root / "data" / "raw", "510500.SH")
    return DailySnapshot(
        "510500.SH",
        canonicalize_daily(data.daily, "510500.SH"),
        canonicalize_daily(execution, "510500.SH"),
        {
            "mode": source["mode"],
            "adjusted_manifest_sha256": source["adjusted_manifest_sha256"],
            "execution_manifest_sha256": source["execution_manifest_sha256"],
        },
    )


def _load_512100(repo_root: Path, protocol: dict[str, object]) -> DailySnapshot:
    source = protocol["sources"]["512100.SH"]
    experiment = repo_root / "experiments" / source["experiment_id"]
    artifacts = experiment / "artifacts"
    validate_experiment_archive(experiment)
    _assert_hash(experiment / "experiment_manifest.json", source["experiment_manifest_sha256"])
    _assert_hash(artifacts / "512100_data_manifest.json", source["data_manifest_sha256"])
    _assert_hash(artifacts / "512100_adjusted_daily.csv", source["adjusted_daily_sha256"])
    _assert_hash(artifacts / "512100_execution_daily.csv", source["execution_daily_sha256"])
    return DailySnapshot(
        "512100.SH",
        canonicalize_daily(pd.read_csv(artifacts / "512100_adjusted_daily.csv"), "512100.SH"),
        canonicalize_daily(pd.read_csv(artifacts / "512100_execution_daily.csv"), "512100.SH"),
        {
            "mode": source["mode"],
            "source_experiment": source["experiment_id"],
            "data_manifest": _read_json(artifacts / "512100_data_manifest.json"),
        },
    )


def _fetch_510300(protocol: dict[str, object], env_file: Path) -> DailySnapshot:
    dataset = protocol["dataset"]
    symbol = "510300.SH"
    adjusted, adjusted_metadata = fetch_etf_ohlcv(
        symbol,
        dataset["start"],
        dataset["development_cutoff"],
        "daily",
        env_file=env_file,
    )
    execution, execution_metadata = fetch_etf_unadjusted_daily(
        symbol,
        dataset["start"],
        dataset["development_cutoff"],
        env_file=env_file,
    )
    return DailySnapshot(
        symbol,
        canonicalize_daily(adjusted, symbol),
        canonicalize_daily(execution, symbol),
        {"adjusted": adjusted_metadata, "execution": execution_metadata},
    )


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
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX20 may only build and validate data evidence")

    snapshots = {
        "510500.SH": _load_510500(repo_root, protocol),
        "512100.SH": _load_512100(repo_root, protocol),
        "510300.SH": _fetch_510300(protocol, repo_root / ".env"),
    }
    dataset = protocol["dataset"]
    clipped, calendar, quality = synchronize_snapshots(
        snapshots,
        dataset["start"],
        dataset["development_cutoff"],
        require_exact_calendar=False,
    )

    for symbol, snapshot in sorted(clipped.items()):
        code = symbol.split(".")[0]
        _write_csv(snapshot.adjusted, artifacts / f"{code}_adjusted_daily.csv")
        _write_csv(snapshot.execution, artifacts / f"{code}_execution_daily.csv")
    _write_csv(calendar, artifacts / "common_calendar.csv")
    source_evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "quality": quality,
        "sources": {symbol: snapshot.metadata for symbol, snapshot in sorted(clipped.items())},
    }
    _write_json(artifacts / "source_evidence.json", source_evidence)

    status = (
        "PASS"
        if quality["exact_calendar_match"] or not dataset["require_exact_calendar"]
        else "FAIL"
    )
    missing_512100 = quality["missing_sessions"].get("512100.SH", [])
    (experiment / "03_execution.md").write_text(
        "# S003 EX20 执行\n\n"
        f"三只ETF均覆盖{quality['common_sessions']}个共同交易日，实际范围为"
        f"{quality['common_first_session']}至{quality['common_last_session']}。"
        "后复权与未复权日线逐日对齐，OHLC、成交量、成交额和复权因子质量门通过。\n\n"
        f"跨标的精确日历质量门结果为`{status}`。512100缺少目标交易日："
        f"{missing_512100}。外部公告确认2022-09-02为512100份额合并日，当日暂停交易；"
        "这属于合法停牌，不是行情接口漏数。\n\n"
        "执行过程没有计算条件未来收益或生成信号。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX20 结论\n\n"
        f"结论：`{status}`。三只ETF的单体数据质量有效，但预注册的完全一致交易日历要求"
        "未通过。512100因2022-09-02份额合并合法停牌一天，不能直接按普通共同日历处理。\n\n"
        "EX20保留为失败证据。下一轮必须在读取条件未来收益前冻结合法停牌的因果对齐规则："
        "以510500交易日历为主，参考标的只使用当时已经发布的最近观测，并显式记录陈旧天数。"
        "该数据门结果不构成任何策略有效性证据。\n",
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
            "common_sessions": quality["common_sessions"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
