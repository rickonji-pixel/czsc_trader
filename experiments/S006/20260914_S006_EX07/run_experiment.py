from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX07"


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
    if protocol.get("reads_new_returns"):
        raise ValueError("EX07 must be a derived correction without new return access")

    source = protocol["source"]
    source_experiment = repo / "experiments/S006" / str(source["experiment_id"])
    validate_experiment_archive(source_experiment)
    if _sha256(source_experiment / "experiment_manifest.json") != str(source["manifest_sha256"]):
        raise ValueError("EX06 manifest differs from frozen source")
    source_ledger = source_experiment / "artifacts/information_path_ledger.csv.gz"
    if _sha256(source_ledger) != str(source["ledger_sha256"]):
        raise ValueError("EX06 ledger differs from frozen source")

    ledger = pd.read_csv(source_ledger)
    before = ledger.copy(deep=True)
    minimum = int(protocol["minimum_hac_observations"])
    mask = ledger["kind"].eq("EVENT_FACTOR") & ledger["confirmation_observations"].lt(minimum)
    ledger.loc[mask, "identifiable"] = False
    ledger.loc[mask, "evidence_label"] = "UNIDENTIFIABLE"
    protected_columns = [column for column in ledger.columns if column not in {"identifiable", "evidence_label"}]
    if not ledger[protected_columns].equals(before[protected_columns]):
        raise ValueError("correction changed protected evidence columns")
    if int(mask.sum()) != 3:
        raise ValueError(f"unexpected corrected path count: {int(mask.sum())}")
    if int(ledger["path_id"].nunique()) != 2040:
        raise ValueError("corrected ledger must preserve all 2040 paths")
    if int(ledger["evidence_label"].eq("FDR_SUPPORTED").sum()) != 7:
        raise ValueError("correction changed FDR-supported paths")

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    corrected = artifacts / "corrected_information_path_ledger.csv.gz"
    ledger.to_csv(corrected, index=False, compression=compression, lineterminator="\n")
    counts = ledger["evidence_label"].value_counts().sort_index()
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "corrected_path_count": int(mask.sum()),
        "corrected_path_ids": sorted(ledger.loc[mask, "path_id"].astype(str)),
        "path_count": int(len(ledger)),
        "fdr_supported_count": int(ledger["evidence_label"].eq("FDR_SUPPORTED").sum()),
        "evidence_label_counts": {str(key): int(value) for key, value in counts.items()},
        "decision": "PROCEED_TO_CROSS_TYPE_COMPLEMENTARITY_REVIEW",
        "source_ledger_sha256": str(source["ledger_sha256"]),
        "corrected_ledger_sha256": _sha256(corrected),
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "correction_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S006 EX07 执行\n\n"
        "状态：`COMPLETE`。派生修订3条新闻事件路径；2,040条路径、全部数值字段和7条"
        "全局FDR支持路径均保持不变。未读取新收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S006 EX07 结论\n\n"
        "裁决：`PROCEED_TO_CROSS_TYPE_COMPLEMENTARITY_REVIEW`。新闻事件确认期只有13个观察，"
        "统一按`UNIDENTIFIABLE`处理；它当前不能支持或反对S006。后续实验只读取修订总账。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
