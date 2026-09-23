from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from dataflows import DataRequest, DataStatus, Dataflows
from dataflows.tushare_common import get_tushare_pro
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX51"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _yearly(pro, endpoint: str, code: str, start_year: int, end_year: int) -> tuple[pd.DataFrame, list[str]]:
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for year in range(start_year, end_year + 1):
        kwargs = {"start_date": f"{year}0101", "end_date": f"{year}1231"}
        if endpoint != "vix_index":
            kwargs["ts_code"] = code
        try:
            frame = getattr(pro, endpoint)(**kwargs)
            data = pd.DataFrame() if frame is None else pd.DataFrame(frame)
            if not data.empty:
                frames.append(data)
        except Exception as exc:
            errors.append(f"{year}:{type(exc).__name__}:{exc}")
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(), errors)


def _vendor_quality(frame: pd.DataFrame, errors: list[str], specification: dict[str, object], start: str, cutoff: str) -> dict[str, object]:
    row: dict[str, object] = {
        "capability_id": specification["id"],
        "endpoint": specification["endpoint"],
        "code": specification["code"],
        "priority": bool(specification["priority"]),
        "vendor_rows": int(len(frame)),
        "vendor_start": "",
        "vendor_cutoff": "",
        "vendor_duplicate_dates": "",
        "vendor_numeric_nulls": "",
        "vendor_errors": " | ".join(errors),
        "vendor_pass": False,
        "dfls_dataset": specification["dfls_dataset"] or "",
        "dfls_status": "MISSING_DATASET" if specification["dfls_dataset"] is None else "",
        "dfls_rows": 0,
        "dfls_start": "",
        "dfls_cutoff": "",
        "dfls_pass": False,
    }
    if frame.empty or errors:
        return row
    dates = pd.to_datetime(frame["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    numeric_columns = [column for column in ("open", "high", "low", "close", "bid_close", "ask_close") if column in frame]
    numeric_nulls = sum(int(pd.to_numeric(frame[column], errors="coerce").isna().sum()) for column in numeric_columns)
    numeric_positive = all((pd.to_numeric(frame[column], errors="coerce") > 0).all() for column in numeric_columns)
    bid_ask_valid = True
    if {"bid_close", "ask_close"}.issubset(frame.columns):
        bid_ask_valid = bool((pd.to_numeric(frame["bid_close"]) <= pd.to_numeric(frame["ask_close"])).all())
    actual_start = dates.min().date().isoformat()
    actual_cutoff = dates.max().date().isoformat()
    row.update({
        "vendor_start": actual_start,
        "vendor_cutoff": actual_cutoff,
        "vendor_duplicate_dates": int(dates.duplicated().sum()),
        "vendor_numeric_nulls": numeric_nulls,
        "vendor_pass": bool(
            actual_start <= start
            and actual_cutoff == cutoff
            and len(frame) >= int(specification["minimum_rows"])
            and not dates.duplicated().any()
            and not dates.isna().any()
            and numeric_nulls == 0
            and numeric_positive
            and bid_ask_valid
        ) if specification["priority"] else bool(actual_cutoff == cutoff),
    })
    return row


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("stores_raw_vendor_data", "reads_target_returns", "reads_sealed_validation", "selects_input", "starts_search", "candidate_generation", "mutates_platform", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("capability gate permissions differ from frozen protocol")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
        "ex50_manifest_sha256": repo / "experiments/S008/20260923_S008_EX50/experiment_manifest.json",
        "dfls_contract_sha256": repo / "packages/dataflows/src/dataflows/contract.py",
        "dfls_facade_sha256": repo / "packages/dataflows/src/dataflows/facade.py",
        "dfls_adapter_sha256": repo / "packages/dataflows/src/dataflows/tushare_strategy_data.py",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")

    pro = get_tushare_pro(repo / ".env")
    dataflows = Dataflows()
    start, cutoff = str(protocol["request_start"]), str(protocol["development_cutoff"])
    start_year, end_year = pd.Timestamp(start).year, pd.Timestamp(cutoff).year
    rows: list[dict[str, object]] = []
    for raw_specification in protocol["capabilities"]:
        specification = dict(raw_specification)
        frame, errors = _yearly(pro, str(specification["endpoint"]), str(specification["code"]), start_year, end_year)
        row = _vendor_quality(frame, errors, specification, start, cutoff)
        if specification["dfls_dataset"] is not None:
            result = dataflows.fetch(DataRequest(
                dataset=str(specification["dfls_dataset"]),
                symbol=str(specification["code"]),
                start=start,
                end=cutoff,
                required_cutoff=cutoff,
                frequency="daily",
                options={"env_file": repo / ".env"},
            ))
            row["dfls_status"] = result.status.value
            if result.status is DataStatus.READY and result.identity is not None:
                row.update({
                    "dfls_rows": int(len(result.dataframe)),
                    "dfls_start": result.identity.data_start,
                    "dfls_cutoff": result.identity.data_cutoff,
                    "dfls_pass": bool(
                        pd.Timestamp(result.identity.data_start).date().isoformat() <= start
                        and pd.Timestamp(result.identity.data_cutoff).date().isoformat() == cutoff
                        and len(result.dataframe) >= int(specification["minimum_rows"])
                        and result.identity.metadata.get("availability_rule") == protocol["causality_rules"]["index_global"]
                    ),
                })
        rows.append(row)

    priority = [row for row in rows if row["priority"]]
    vendor_failures = [str(row["capability_id"]) for row in priority if not row["vendor_pass"]]
    dfls_failures = [str(row["capability_id"]) for row in priority if not row["dfls_pass"]]
    if vendor_failures:
        decision = protocol["adjudication"]["vendor_failure"]
    elif dfls_failures:
        decision = protocol["adjudication"]["dfls_failure"]
    else:
        decision = protocol["adjudication"]["all_pass"]

    with (artifacts / "capability_audit.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "priority_capability_count": len(priority),
        "vendor_pass_count": sum(bool(row["vendor_pass"]) for row in priority),
        "dfls_pass_count": sum(bool(row["dfls_pass"]) for row in priority),
        "vendor_failures": vendor_failures,
        "dfls_failures": dfls_failures,
        "catalog_exclusions": protocol["catalog_exclusions"],
        "stores_raw_vendor_data": False,
        "reads_target_returns": False,
        "reads_sealed_validation": False,
        "candidate_created": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "capability_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S008 EX51 执行记录\n\n"
        f"完成{len(rows)}项Tushare能力审计；优先能力供应商通过={summary['vendor_pass_count']}/{len(priority)}，"
        f"DFLS通过={summary['dfls_pass_count']}/{len(priority)}。只归档能力与身份摘要，没有保存供应商"
        "原始数据、读取目标收益或密封池。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX51 结论\n\n"
        f"机器裁决：`{decision}`。供应商失败={vendor_failures or '无'}；DFLS失败="
        f"{dfls_failures or '无'}。只有平台数据合同补齐并通过后，才允许进入全球风险信息审计。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "credential_id": protocol["credential_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
