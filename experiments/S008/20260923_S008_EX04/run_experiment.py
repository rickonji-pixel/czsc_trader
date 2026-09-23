from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX04"


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_command(command: list[str], *, cwd: Path) -> dict[str, object]:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    payload: object | None = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "payload": payload,
    }


def _command_passed(result: dict[str, object]) -> bool:
    payload = result.get("payload")
    return (
        result.get("returncode") == 0
        and isinstance(payload, dict)
        and payload.get("status") == "PASS"
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
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("data manifest files are invalid")
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
    dates = pd.to_datetime(frame[date_column], errors="raise").dt.normalize()
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
    activity = frame[["volume", "amount"]].apply(pd.to_numeric, errors="raise")
    if not (activity >= 0).all().all():
        raise ValueError("volume and amount must be non-negative")
    return dates


def _finalize(
    experiment: Path,
    protocol: dict[str, object],
    *,
    status: str,
    decision: str,
    report: dict[str, object],
    execution_text: str,
    conclusion_text: str,
) -> None:
    artifacts = experiment / "artifacts"
    _write_json(artifacts / "data_gate_report.json", report)
    (experiment / "03_execution.md").write_text(execution_text, encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(conclusion_text, encoding="utf-8")
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": status,
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
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
        "reads_primary_sealed_validation",
        "reads_strategy_returns",
        "observes_price_path_for_selection",
        "signal_selected",
        "parameter_search_started",
        "candidate_generation",
        "promotion_allowed",
        "modifies_platform_modules",
        "mutates_frozen_strategy",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("data gate protocol exceeds its authorized scope")
    if str(protocol["requested_end"]) >= str(protocol["prohibited_start"]):
        raise ValueError("external replication data overlaps the prohibited period")

    cli = repo / ".venv" / "Scripts" / "czsc-trader.exe"
    prepare = _run_command(
        [
            str(cli), "data", "prepare", "--symbol", str(protocol["symbol"]),
            "--asset", str(protocol["asset_type"]), "--start", str(protocol["requested_start"]),
            "--end", str(protocol["requested_end"]), "--repo-root", str(repo),
        ],
        cwd=repo,
    )
    _write_json(artifacts / "data_prepare_result.json", prepare)
    validate: dict[str, object] | None = None
    if _command_passed(prepare):
        validate = _run_command(
            [
                str(cli), "data", "validate", "--symbol", str(protocol["symbol"]),
                "--repo-root", str(repo),
            ],
            cwd=repo,
        )
        _write_json(artifacts / "data_validate_result.json", validate)
    if not _command_passed(prepare) or validate is None or not _command_passed(validate):
        failed = "data prepare" if not _command_passed(prepare) else "data validate"
        report = {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "status": "FAIL",
            "decision": protocol["adjudication"]["fail"],
            "failed_step": failed,
            "strategy_returns_computed": False,
            "price_path_observed_for_selection": False,
            "parameter_search_started": False,
            "candidate_created": False,
            "pte_mutated": False,
        }
        _finalize(
            experiment,
            protocol,
            status="FAIL",
            decision=str(protocol["adjudication"]["fail"]),
            report=report,
            execution_text=(
                "# S008 EX04 执行记录\n\n"
                f"按预注册入口执行至`{failed}`时未通过，已停止；未计算策略收益或选择参数。\n"
            ),
            conclusion_text=(
                "# S008 EX04 结论\n\n"
                "裁决：`FAIL_REPLICATION_DATA_GATE`。518800.SH外部复现数据门未通过，"
                "禁止启动机制复制实验；本实验没有Alpha、信号、参数或候选结论。\n"
            ),
        )
        return

    source_paths = {
        "research_manifest": raw / "518800_manifest.json",
        "execution_manifest": raw / "518800_execution_manifest.json",
        "validation": raw / "518800_validation.json",
    }
    if not all(path.is_file() for path in source_paths.values()):
        raise ValueError("successful data commands did not publish all declared evidence")
    source = {key: _read_object(path) for key, path in source_paths.items()}
    research_manifest = source["research_manifest"]
    execution_manifest = source["execution_manifest"]
    validation_evidence = source["validation"]
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
    if validation_evidence.get("status") != "PASS":
        raise ValueError("project data validation did not pass")

    daily = _load_frequency(raw, research_manifest, "daily")
    execution = _load_frequency(raw, execution_manifest, "daily")
    daily_dates = _validate_ohlcv(daily, "date")
    execution_dates = _validate_ohlcv(execution, "date")
    if not daily_dates.equals(execution_dates):
        raise ValueError("adjusted and execution daily sessions differ")
    expected_start = pd.Timestamp(str(protocol["requested_start"]))
    expected_end = pd.Timestamp(str(protocol["requested_end"]))
    if daily_dates.iloc[0] != expected_start or daily_dates.iloc[-1] != expected_end:
        raise ValueError("daily coverage does not match the preregistered boundary")
    if (daily_dates >= pd.Timestamp(str(protocol["prohibited_start"]))).any():
        raise ValueError("loaded data overlaps the prohibited period")

    identities = {key: _sha256(path) for key, path in source_paths.items()}
    for key, path in source_paths.items():
        target = artifacts / f"source_{path.name}"
        target.write_bytes(path.read_bytes())

    materials_path = repo / "research" / "S008" / "materials.json"
    materials = _read_object(materials_path)
    if materials.get("strategy_id") != "S008" or materials.get("symbol") != "518880.SH":
        raise ValueError("primary material registry identity differs")
    materials["external_replication_materials"] = {
        "purpose": "EX03 Donchian机制的同类ETF独立复制",
        "symbol": protocol["symbol"],
        "development_start": protocol["development_start"],
        "development_cutoff": protocol["development_cutoff"],
        "prohibited_start": protocol["prohibited_start"],
        "research_data_manifest": {
            "path": "data/raw/518800_manifest.json",
            "sha256": identities["research_manifest"],
        },
        "execution_data_manifest": {
            "path": "data/raw/518800_execution_manifest.json",
            "sha256": identities["execution_manifest"],
        },
        "validation_evidence": {
            "path": "data/raw/518800_validation.json",
            "sha256": identities["validation"],
        },
    }
    _write_json(materials_path, materials)

    report = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": protocol["adjudication"]["pass"],
        "symbol": protocol["symbol"],
        "instrument_name": research_manifest.get("name"),
        "vendor": research_manifest.get("vendor"),
        "adjustment": research_manifest.get("adjustment"),
        "first_session": daily_dates.iloc[0].date().isoformat(),
        "last_session": daily_dates.iloc[-1].date().isoformat(),
        "daily_sessions": int(daily_dates.nunique()),
        "intraday_bars": validation_evidence.get("intraday", {}).get("bar_count"),
        "intraday_complete_sessions": validation_evidence.get("intraday", {}).get("complete_day_count"),
        "weekly_matched_periods": validation_evidence.get("reconciliation", {}).get("weekly_matched_periods"),
        "source_sha256": identities,
        "strategy_returns_computed": False,
        "price_path_observed_for_selection": False,
        "parameter_search_started": False,
        "candidate_created": False,
        "pte_mutated": False,
    }
    _finalize(
        experiment,
        protocol,
        status="COMPLETE",
        decision=str(protocol["adjudication"]["pass"]),
        report=report,
        execution_text=(
            "# S008 EX04 执行记录\n\n"
            "按预注册命令准备并校验518800.SH截至2024-12-31的数据。随后只检查文件身份、"
            "日期覆盖和OHLCV结构；没有读取2025年后的同源数据，没有计算策略收益或选择参数。\n"
        ),
        conclusion_text=(
            "# S008 EX04 结论\n\n"
            "裁决：`PASS_REPLICATION_DATA_GATE`。518800.SH外部复现数据覆盖预注册开发期，"
            "文件身份、交易日、OHLCV及项目频率对账全部通过。通过只代表数据可用；"
            "下一步仍须人工评审后才能按EX03冻结规则执行机制复制。\n"
        ),
    )


if __name__ == "__main__":
    main()
