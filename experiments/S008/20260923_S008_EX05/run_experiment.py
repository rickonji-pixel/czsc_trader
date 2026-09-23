from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX05"


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


def _run(command: list[str], repo: Path) -> dict[str, object]:
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        payload = None
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": completed.stderr.strip(),
        "payload": payload,
    }


def _passed(result: dict[str, object]) -> bool:
    payload = result.get("payload")
    return (
        result.get("returncode") == 0
        and isinstance(payload, dict)
        and payload.get("status") == "PASS"
    )


def _verify_files(raw: Path, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest files are missing")
    for filename, identity in files.items():
        if not isinstance(identity, dict):
            raise ValueError(f"invalid file identity: {filename}")
        path = raw / str(filename)
        if not path.is_file() or _sha256(path) != identity.get("sha256"):
            raise ValueError(f"file identity differs: {path}")


def _load_daily(raw: Path, manifest: dict[str, object]) -> pd.DataFrame:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("manifest files are invalid")
    paths = [
        raw / str(filename)
        for filename, identity in files.items()
        if isinstance(identity, dict) and identity.get("frequency") == "daily"
    ]
    if not paths:
        raise ValueError("manifest has no daily files")
    return pd.concat([pd.read_csv(path) for path in sorted(paths)], ignore_index=True)


def _validate_daily(frame: pd.DataFrame) -> pd.Series:
    required = {"date", "open", "high", "low", "close", "volume", "amount"}
    if not required.issubset(frame.columns):
        raise ValueError(f"daily columns are missing: {sorted(required - set(frame.columns))}")
    dates = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    if not dates.is_unique or not dates.is_monotonic_increasing:
        raise ValueError("daily dates must be unique and increasing")
    prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="raise")
    activity = frame[["volume", "amount"]].apply(pd.to_numeric, errors="raise")
    if not (prices > 0).all().all() or not (activity >= 0).all().all():
        raise ValueError("daily OHLCV contains invalid numeric values")
    if not (
        (prices["high"] >= prices[["open", "close", "low"]].max(axis=1))
        & (prices["low"] <= prices[["open", "close", "high"]].min(axis=1))
    ).all():
        raise ValueError("daily OHLC relationships are invalid")
    return dates


def _repair_record(manifest: dict[str, object]) -> dict[str, object]:
    metadata = manifest.get("fetch_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("frequency metadata is missing")
    intraday = metadata.get("30m")
    if not isinstance(intraday, dict):
        raise ValueError("30m metadata is missing")
    records = intraday.get("repair_records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("30m repair record is missing or ambiguous")
    return records[0]


def _finalize(
    experiment: Path,
    protocol: dict[str, object],
    *,
    status: str,
    decision: str,
    report: dict[str, object],
    execution: str,
    conclusion: str,
) -> None:
    _write_json(experiment / "artifacts" / "data_gate_report.json", report)
    (experiment / "03_execution.md").write_text(execution, encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(conclusion, encoding="utf-8")
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
        "mutates_frozen_strategy",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("protocol exceeds the authorized data gate")
    if str(protocol["requested_end"]) >= str(protocol["prohibited_start"]):
        raise ValueError("replication data overlaps the prohibited period")

    cli = repo / ".venv" / "Scripts" / "czsc-trader.exe"
    prepare = _run(
        [
            str(cli), "data", "prepare", "--symbol", str(protocol["symbol"]),
            "--asset", str(protocol["asset_type"]), "--start", str(protocol["requested_start"]),
            "--end", str(protocol["requested_end"]), "--repo-root", str(repo),
        ],
        repo,
    )
    _write_json(artifacts / "data_prepare_result.json", prepare)
    validate: dict[str, object] | None = None
    if _passed(prepare):
        validate = _run(
            [str(cli), "data", "validate", "--symbol", str(protocol["symbol"]), "--repo-root", str(repo)],
            repo,
        )
        _write_json(artifacts / "data_validate_result.json", validate)
    if not _passed(prepare) or validate is None or not _passed(validate):
        failed_step = "data prepare" if not _passed(prepare) else "data validate"
        decision = str(protocol["adjudication"]["fail"])
        _finalize(
            experiment,
            protocol,
            status="FAIL",
            decision=decision,
            report={
                "schema_version": 1,
                "experiment_id": EXPERIMENT_ID,
                "status": "FAIL",
                "decision": decision,
                "failed_step": failed_step,
                "strategy_returns_computed": False,
                "candidate_created": False,
            },
            execution=f"# S008 EX05 执行记录\n\n正式`{failed_step}`未通过，实验停止。\n",
            conclusion=(
                "# S008 EX05 结论\n\n裁决：`FAIL_REPLICATION_DATA_GATE`。"
                "禁止启动机制复制；没有产生Alpha、参数或候选结论。\n"
            ),
        )
        return

    source_paths = {
        "research_manifest": raw / "518800_manifest.json",
        "execution_manifest": raw / "518800_execution_manifest.json",
        "validation": raw / "518800_validation.json",
    }
    if not all(path.is_file() for path in source_paths.values()):
        raise ValueError("successful commands did not publish all evidence")
    source = {key: _read_object(path) for key, path in source_paths.items()}
    research_manifest = source["research_manifest"]
    execution_manifest = source["execution_manifest"]
    validation_evidence = source["validation"]
    for manifest in (research_manifest, execution_manifest):
        if manifest.get("symbol") != protocol["symbol"]:
            raise ValueError("manifest symbol differs from protocol")
        if manifest.get("requested_start") != protocol["requested_start"]:
            raise ValueError("manifest start differs from protocol")
        if manifest.get("requested_end") != protocol["requested_end"]:
            raise ValueError("manifest end differs from protocol")
        _verify_files(raw, manifest)
    if validation_evidence.get("status") != "PASS":
        raise ValueError("published validation evidence did not pass")

    daily = _load_daily(raw, research_manifest)
    execution_daily = _load_daily(raw, execution_manifest)
    dates = _validate_daily(daily)
    execution_dates = _validate_daily(execution_daily)
    if not dates.equals(execution_dates):
        raise ValueError("research and execution daily sessions differ")
    if dates.iloc[0] != pd.Timestamp(protocol["requested_start"]):
        raise ValueError("daily start differs from protocol")
    if dates.iloc[-1] != pd.Timestamp(protocol["requested_end"]):
        raise ValueError("daily end differs from protocol")
    if (dates >= pd.Timestamp(protocol["prohibited_start"])).any():
        raise ValueError("loaded data overlaps the prohibited period")

    repair = _repair_record(research_manifest)
    if repair.get("patch_id") != protocol["required_patch_id"]:
        raise ValueError("repair patch id differs from protocol")
    if repair.get("affected_date_count") != protocol["required_affected_date_count"]:
        raise ValueError("repair affected-date count differs from protocol")

    identities = {key: _sha256(path) for key, path in source_paths.items()}
    for path in source_paths.values():
        (artifacts / f"source_{path.name}").write_bytes(path.read_bytes())

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
        "required_patch_id": protocol["required_patch_id"],
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

    decision = str(protocol["adjudication"]["pass"])
    report = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "symbol": protocol["symbol"],
        "first_session": dates.iloc[0].date().isoformat(),
        "last_session": dates.iloc[-1].date().isoformat(),
        "daily_sessions": int(dates.nunique()),
        "repair_patch_id": repair["patch_id"],
        "repair_affected_date_count": repair["affected_date_count"],
        "source_sha256": identities,
        "strategy_returns_computed": False,
        "price_path_observed_for_selection": False,
        "parameter_search_started": False,
        "candidate_created": False,
    }
    _finalize(
        experiment,
        protocol,
        status="COMPLETE",
        decision=decision,
        report=report,
        execution=(
            "# S008 EX05 执行记录\n\n通过正式入口重新准备并校验518800.SH开发期数据；"
            "随后只验证文件身份、日期、OHLCV和补丁记录，没有计算策略收益或选择参数。\n"
        ),
        conclusion=(
            "# S008 EX05 结论\n\n裁决：`PASS_REPLICATION_DATA_GATE`。"
            "518800.SH外部复现材料通过数据门，下一步仍须人工评审后才能执行机制复制。\n"
        ),
    )


if __name__ == "__main__":
    main()
