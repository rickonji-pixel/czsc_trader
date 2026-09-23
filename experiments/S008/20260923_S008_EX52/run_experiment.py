from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from dataflows import DataRequest, DataStatus, Dataflows
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX52"


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "stores_raw_vendor_data",
        "reads_target_returns",
        "selects_input",
        "selects_template",
        "starts_search",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("data gate cannot save raw data, select, search, promote or mutate")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
        "ex51_manifest_sha256": repo
        / "experiments/S008/20260923_S008_EX51/experiment_manifest.json",
        "dfls_contract_sha256": repo / "packages/dataflows/src/dataflows/contract.py",
        "dfls_facade_sha256": repo / "packages/dataflows/src/dataflows/facade.py",
        "dfls_adapter_sha256": repo
        / "packages/dataflows/src/dataflows/tushare_strategy_data.py",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")

    dataflows = Dataflows()
    rows: list[dict[str, object]] = []
    publication_failures: list[str] = []
    data_failures: list[str] = []
    causality_failures: list[str] = []
    for raw_specification in protocol["datasets"]:
        specification = dict(raw_specification)
        capability_id = str(specification["capability_id"])
        result = dataflows.fetch(
            DataRequest(
                dataset=str(specification["dataset"]),
                symbol=str(specification["symbol"]),
                start=str(protocol["development_start"]),
                end=str(protocol["development_cutoff"]),
                required_cutoff=str(protocol["development_cutoff"]),
                frequency="daily",
                options={"env_file": repo / ".env"},
            )
        )
        row: dict[str, object] = {
            "capability_id": capability_id,
            "dataset": specification["dataset"],
            "symbol": specification["symbol"],
            "status": result.status.value,
            "rows": 0,
            "data_start": "",
            "data_cutoff": "",
            "content_sha256": "",
            "duplicate_dates": "",
            "maximum_required_null_rate": "",
            "availability_rule": "",
            "maximum_start_lag_days": "",
            "data_contract_pass": False,
            "causality_metadata_pass": False,
            "error_code": result.error.code if result.error else "",
        }
        if result.status is not DataStatus.READY:
            publication_failures.append(capability_id)
            rows.append(row)
            continue

        frame = result.dataframe
        identity = result.identity
        if identity is None:
            raise AssertionError("READY result must carry identity")
        required_fields = [str(item) for item in specification["required_fields"]]
        missing_fields = sorted(set(required_fields).difference(frame.columns))
        maximum_null_rate = (
            1.0
            if missing_fields
            else max(float(frame[column].isna().mean()) for column in required_fields)
        )
        finite = not missing_fields and all(
            np.isfinite(pd.to_numeric(frame[column], errors="coerce").to_numpy()).all()
            for column in required_fields
        )
        duplicate_dates = int(frame.duplicated(["Date"]).sum())
        actual_start = pd.Timestamp(identity.data_start).normalize()
        actual_cutoff = pd.Timestamp(identity.data_cutoff).normalize()
        metadata = dict(identity.metadata)
        data_contract_pass = bool(
            len(frame) >= int(specification["minimum_rows"])
            and actual_start == pd.Timestamp(str(protocol["development_start"]))
            and actual_cutoff == pd.Timestamp(str(protocol["development_cutoff"]))
            and not missing_fields
            and maximum_null_rate == 0.0
            and finite
            and duplicate_dates == 0
            and len(identity.content_sha256) == 64
            and identity.dataset == str(specification["dataset"])
            and identity.source == "tushare"
            and metadata.get("vendor_symbol") == specification["symbol"]
        )
        causality_metadata_pass = bool(
            metadata.get("availability_rule") == specification["availability_rule"]
            and metadata.get("maximum_start_lag_days") == 10
            and (
                "vendor_timezone" not in specification
                or metadata.get("vendor_timezone") == specification["vendor_timezone"]
            )
        )
        if not data_contract_pass:
            data_failures.append(capability_id)
        if not causality_metadata_pass:
            causality_failures.append(capability_id)
        row.update(
            {
                "rows": len(frame),
                "data_start": identity.data_start,
                "data_cutoff": identity.data_cutoff,
                "content_sha256": identity.content_sha256,
                "duplicate_dates": duplicate_dates,
                "maximum_required_null_rate": maximum_null_rate,
                "availability_rule": metadata.get("availability_rule", ""),
                "maximum_start_lag_days": metadata.get("maximum_start_lag_days", ""),
                "data_contract_pass": data_contract_pass,
                "causality_metadata_pass": causality_metadata_pass,
            }
        )
        rows.append(row)

    if publication_failures:
        decision = protocol["adjudication"]["publication_failure"]
    elif data_failures:
        decision = protocol["adjudication"]["data_failure"]
    elif causality_failures:
        decision = protocol["adjudication"]["causality_failure"]
    else:
        decision = protocol["adjudication"]["all_pass"]

    with (artifacts / "dfls_gate.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "predecessor_experiment_id": protocol["predecessor_experiment_id"],
        "decision": decision,
        "dataset_count": len(rows),
        "ready_count": sum(row["status"] == DataStatus.READY.value for row in rows),
        "data_contract_pass_count": sum(bool(row["data_contract_pass"]) for row in rows),
        "causality_metadata_pass_count": sum(
            bool(row["causality_metadata_pass"]) for row in rows
        ),
        "publication_failures": publication_failures,
        "data_failures": data_failures,
        "causality_failures": causality_failures,
        "stores_raw_vendor_data": False,
        "reads_target_returns": False,
        "reads_sealed_validation": False,
        "candidate_created": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "gate_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S008 EX52 执行记录\n\n"
        f"通过DFLS公开门面请求{len(rows)}项全球风险数据；READY={summary['ready_count']}，"
        f"数据合同通过={summary['data_contract_pass_count']}，因果元数据通过="
        f"{summary['causality_metadata_pass_count']}。只归档身份和检查摘要，没有保存供应商原始"
        "数据、读取518880.SH目标收益或封存验证池。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX52 结论\n\n"
        f"机器裁决：`{decision}`。发布失败={publication_failures or '无'}；数据合同失败="
        f"{data_failures or '无'}；因果元数据失败={causality_failures or '无'}。只有全部通过时，"
        "下一步才允许开展全球风险信息审计；本实验不形成任何Alpha、策略或候选证据。\n",
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
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
