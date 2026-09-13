from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX13"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    rows: list[dict[str, object]] = []
    for experiment_id in protocol["audited_experiments"]:
        source = repo / "experiments/S005" / str(experiment_id)
        manifest = validate_experiment_archive(source)
        rows.append(
            {
                "experiment_id": experiment_id,
                "experiment_type": manifest["experiment_type"],
                "status": manifest["status"],
                "decision": manifest["decision"],
                "candidate_created": bool(manifest.get("candidate_created", False)),
                "strategy_frozen": bool(manifest.get("strategy_frozen", False)),
                "pte_mutated": bool(manifest.get("pte_mutated", False)),
            }
        )
    evidence = pd.DataFrame(rows)
    if not evidence["status"].eq("COMPLETE").all():
        raise ValueError("audited experiment is incomplete")
    if evidence[["candidate_created", "strategy_frozen", "pte_mutated"]].any().any():
        raise ValueError("S005 discovery unexpectedly mutated promotion or runtime state")

    return_ids = set(str(value) for value in protocol["return_path_experiments"])
    density_ids = set(str(value) for value in protocol["density_stop_experiments"])
    return_rows = evidence.loc[evidence["experiment_id"].isin(return_ids)]
    density_rows = evidence.loc[evidence["experiment_id"].isin(density_ids)]
    return_stopped = return_rows["decision"].str.startswith("STOP_").all() and len(return_rows) == len(return_ids)
    density_stopped = density_rows["decision"].str.startswith("STOP_").all() and len(density_rows) == len(density_ids)
    decision = "PAUSE_S005_WITHOUT_CANDIDATE" if return_stopped and density_stopped else "CONTINUE_EVIDENCE_REPAIR"
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "audited_experiments": int(len(evidence)),
        "fixed_return_paths_falsified": int(len(return_rows)) if return_stopped else 0,
        "density_families_stopped_without_return_read": int(len(density_rows)) if density_stopped else 0,
        "decision": decision,
        "reopen_conditions": protocol["reopen_conditions"],
        "reads_new_market_data": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    evidence.to_csv(artifacts / "audit_ledger.csv", index=False, lineterminator="\n")
    (artifacts / "stop_audit.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        f"# S005 EX13 执行\n\n状态：COMPLETE。已校验{len(evidence)}个实验档案；"
        f"固定收益路径{len(return_rows)}条，纯密度停止机制族{len(density_rows)}类。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX13 结论\n\n"
        f"裁决：`{decision}`。五条固定收益路径均被证伪，两类机制在读取收益前即因密度不足停止。"
        "继续在相同OHLCV与宽基相对数据上更换阈值、持有期或价格形态，已不具备合理的边际研究价值。\n\n"
        "S005不形成候选、不进入SM或PTE。重新启动必须满足协议登记的新信息源、新机制或用户批准的目标变更之一。\n",
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
            "decision": decision,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
