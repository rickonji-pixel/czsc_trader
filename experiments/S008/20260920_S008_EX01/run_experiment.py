from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd

from dataflows.tushare_etf import fetch_etf_ohlcv

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260920_S008_EX01"


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _verify_manifest_files(raw: Path, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("data manifest has no files")
    for filename, identity in files.items():
        if not isinstance(identity, dict):
            raise ValueError(f"invalid identity for {filename}")
        path = raw / str(filename)
        if not path.is_file():
            raise ValueError(f"missing manifest file: {path}")
        if _sha256(path) != str(identity.get("sha256")):
            raise ValueError(f"manifest hash differs: {path}")


def _load_frequency(raw: Path, manifest: dict[str, object], frequency: str) -> pd.DataFrame:
    files = manifest["files"]
    selected = [
        raw / str(filename)
        for filename, identity in files.items()
        if isinstance(identity, dict) and identity.get("frequency") == frequency
    ]
    if not selected:
        raise ValueError(f"manifest has no {frequency} files")
    return pd.concat([pd.read_csv(path) for path in sorted(selected)], ignore_index=True)


def _validate_ohlcv(frame: pd.DataFrame, date_column: str) -> pd.Series:
    required = {date_column, "open", "high", "low", "close", "volume", "amount"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing columns: {missing}")
    dates = pd.to_datetime(frame[date_column], errors="raise")
    if not dates.is_monotonic_increasing or not dates.is_unique:
        raise ValueError(f"{date_column} must be strictly increasing and unique")
    prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="raise")
    if not (prices > 0).all().all():
        raise ValueError("OHLC prices must be positive")
    if not (
        (prices["high"] >= prices[["open", "close", "low"]].max(axis=1))
        & (prices["low"] <= prices[["open", "close", "high"]].min(axis=1))
    ).all():
        raise ValueError("OHLC relationship is invalid")
    if not (frame[["volume", "amount"]].apply(pd.to_numeric, errors="raise") >= 0).all().all():
        raise ValueError("volume and amount must be non-negative")
    return dates.dt.normalize()


def _finalize_failed_data_gate(
    experiment: Path,
    repo: Path,
    artifacts: Path,
    protocol: dict[str, object],
) -> None:
    frame, metadata = fetch_etf_ohlcv(
        str(protocol["symbol"]),
        str(protocol["requested_start"]),
        str(protocol["requested_end"]),
        "30m",
        env_file=repo / ".env",
    )
    prices = frame[["Open", "High", "Low", "Close"]]
    checks = pd.DataFrame(
        {
            "non_positive": (prices <= 0).any(axis=1),
            "high_breach": frame["High"] < frame[["Open", "Close"]].max(axis=1),
            "low_breach": frame["Low"] > frame[["Open", "Close"]].min(axis=1),
            "range_breach": frame["High"] < frame["Low"],
            "negative_activity": (frame[["Volume", "Amount"]] < 0).any(axis=1),
        }
    )
    invalid = checks.any(axis=1)
    invalid_rows = pd.concat(
        [frame.loc[invalid].reset_index(drop=True), checks.loc[invalid].reset_index(drop=True)],
        axis=1,
    )
    if invalid_rows.empty:
        raise ValueError("data prepare failed but the preregistered OHLCV diagnosis found no defect")
    invalid_rows.to_csv(artifacts / "invalid_30m_rows.csv", index=False, encoding="utf-8")
    invalid_dates = sorted(
        pd.to_datetime(invalid_rows["Date"]).dt.strftime("%Y-%m-%d").unique().tolist()
    )
    report = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "FAIL",
        "decision": "FAIL_DATA_GATE",
        "failed_command": protocol["data_commands"][0],
        "error_code": "market_data_preparation_failed",
        "error_message": "30m: invalid OHLCV relationships",
        "published_files": 0,
        "diagnostic_scope": "30m OHLCV structural validity only",
        "fetched_30m_rows": int(len(frame)),
        "first_timestamp": pd.Timestamp(frame["Date"].min()).isoformat(),
        "last_timestamp": pd.Timestamp(frame["Date"].max()).isoformat(),
        "invalid_rows": int(invalid.sum()),
        "non_positive_rows": int(checks["non_positive"].sum()),
        "high_breach_rows": int(checks["high_breach"].sum()),
        "low_breach_rows": int(checks["low_breach"].sum()),
        "range_breach_rows": int(checks["range_breach"].sum()),
        "negative_activity_rows": int(checks["negative_activity"].sum()),
        "invalid_dates": invalid_dates,
        "vendor_metadata": metadata,
        "root_cause": "12个开发池日期的10:00合并柱含零Open和零Low；现有严格校验拒绝发布",
        "strategy_returns_computed": False,
        "price_path_observed_for_selection": False,
        "signal_selected": False,
        "parameter_search_started": False,
        "candidate_created": False,
        "platform_modules_modified": False,
        "frozen_strategy_mutated": False,
        "pte_mutated": False,
    }
    _write_json(artifacts / "data_gate_report.json", report)
    (experiment / "03_execution.md").write_text(
        "# S008 EX01 执行记录\n\n"
        "按预注册命令调用现有数据管线。`data prepare`明确返回`FAIL`：518880.SH的30分钟数据"
        "存在无效OHLCV关系，原子发布未产生518880文件。只读诊断定位到开发池内12个日期的"
        "10:00合并柱，其Open和Low为零；未计算策略收益，也未修改平台模块。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX01 结论\n\n"
        "裁决：`FAIL_DATA_GATE`。现有数据管线正确拒绝发布518880.SH数据，后续机制研究暂停。"
        "根因是12个开发池日期的30分钟10:00合并柱含零Open和零Low。修复需要修改平台的数据"
        "适配或校正规则，必须先经用户批准；本实验没有Alpha、信号、参数或候选结论。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "FAIL",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": report["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    raw = repo / "data" / "raw"
    protocol = _read_object(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_strategy_returns",
        "observes_price_path_for_selection",
        "candidate_generation",
        "promotion_allowed",
        "modifies_platform_modules",
        "mutates_frozen_strategy",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("data gate protocol exceeds its authorized scope")

    source_paths = {
        "research_manifest": raw / "518880_manifest.json",
        "execution_manifest": raw / "518880_execution_manifest.json",
        "validation": raw / "518880_validation.json",
    }
    if not all(path.is_file() for path in source_paths.values()):
        _finalize_failed_data_gate(experiment, repo, artifacts, protocol)
        return
    source = {key: _read_object(path) for key, path in source_paths.items()}
    research_manifest = source["research_manifest"]
    execution_manifest = source["execution_manifest"]
    validation = source["validation"]

    for manifest in (research_manifest, execution_manifest):
        if manifest.get("symbol") != protocol["symbol"]:
            raise ValueError("data manifest symbol differs from protocol")
        if manifest.get("asset_type") != protocol["asset_type"]:
            raise ValueError("data manifest asset type differs from protocol")
        if manifest.get("requested_start") != protocol["requested_start"]:
            raise ValueError("data manifest requested start differs from protocol")
        if manifest.get("requested_end") != protocol["requested_end"]:
            raise ValueError("data manifest requested end differs from protocol")
        _verify_manifest_files(raw, manifest)
    if validation.get("status") != "PASS":
        raise ValueError("project data validation did not pass")

    daily = _load_frequency(raw, research_manifest, "daily")
    execution = _load_frequency(raw, execution_manifest, "daily")
    daily_dates = _validate_ohlcv(daily, "date")
    execution_dates = _validate_ohlcv(execution, "date")
    if not daily_dates.equals(execution_dates):
        raise ValueError("adjusted and execution daily sessions differ")

    expected_start = pd.Timestamp(str(protocol["requested_start"]))
    expected_end = pd.Timestamp(str(protocol["requested_end"]))
    development_cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    sealed_start = pd.Timestamp(str(protocol["sealed_validation_start"]))
    if daily_dates.iloc[0] != expected_start or daily_dates.iloc[-1] != expected_end:
        raise ValueError("daily coverage does not match the preregistered boundary")
    development_rows = int(daily_dates.between(expected_start, development_cutoff).sum())
    sealed_rows = int(daily_dates.between(sealed_start, expected_end).sum())
    if development_rows == 0 or sealed_rows == 0:
        raise ValueError("development or sealed validation segment is empty")

    identities = {key: _sha256(path) for key, path in source_paths.items()}
    for key, path in source_paths.items():
        shutil.copyfile(path, artifacts / f"source_{path.name}")

    report = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "PASS_DATA_GATE",
        "symbol": protocol["symbol"],
        "instrument_name": research_manifest.get("name"),
        "vendor": research_manifest.get("vendor"),
        "adjustment": research_manifest.get("adjustment"),
        "first_session": daily_dates.iloc[0].date().isoformat(),
        "last_session": daily_dates.iloc[-1].date().isoformat(),
        "daily_sessions": int(daily_dates.nunique()),
        "development_sessions": development_rows,
        "sealed_validation_sessions": sealed_rows,
        "intraday_bars": validation.get("intraday", {}).get("bar_count"),
        "intraday_complete_sessions": validation.get("intraday", {}).get("complete_day_count"),
        "weekly_matched_periods": validation.get("reconciliation", {}).get("weekly_matched_periods"),
        "source_sha256": identities,
        "strategy_returns_computed": False,
        "price_path_observed_for_selection": False,
        "signal_selected": False,
        "parameter_search_started": False,
        "candidate_created": False,
        "platform_modules_modified": False,
        "frozen_strategy_mutated": False,
        "pte_mutated": False
    }
    _write_json(artifacts / "data_gate_report.json", report)

    materials = {
        "schema_version": 1,
        "strategy_id": "S008",
        "credential_id": "SGC-S008-001",
        "symbol": "518880.SH",
        "development_start": protocol["development_start"],
        "development_cutoff": protocol["development_cutoff"],
        "sealed_validation_start": protocol["sealed_validation_start"],
        "sealed_validation_end": protocol["sealed_validation_end"],
        "sealed_validation_policy": "候选公式、参数和执行规则完全固定后仅使用一次",
        "research_data_manifest": {
            "path": "data/raw/518880_manifest.json",
            "sha256": identities["research_manifest"]
        },
        "execution_data_manifest": {
            "path": "data/raw/518880_execution_manifest.json",
            "sha256": identities["execution_manifest"]
        },
        "validation_evidence": {
            "path": "data/raw/518880_validation.json",
            "sha256": identities["validation"]
        }
    }
    _write_json(repo / "research" / "S008" / "materials.json", materials)

    (experiment / "03_execution.md").write_text(
        "# S008 EX01 执行记录\n\n"
        "按预注册命令准备518880.SH研究数据并调用项目现有数据校验入口。随后仅检查文件身份、"
        "日期覆盖、OHLCV结构和开发池/封存验证池分段，没有计算策略收益或观察价格路径来选择规则。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX01 结论\n\n"
        "裁决：`PASS_DATA_GATE`。518880.SH受控研究数据与不复权执行数据覆盖预注册区间，"
        "文件身份、交易日、OHLCV及项目频率对账全部通过。开发池和封存验证池均已建立；"
        "本实验没有产生Alpha、信号、参数或候选结论。下一步须经人工评审后才能启动机制实验。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": report["decision"],
            "promotion_allowed": False
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
