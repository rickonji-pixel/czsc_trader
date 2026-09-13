from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S001_EX12"


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
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    contract_path = repo / str(protocol["source_contract_result"])
    health_path = repo / str(protocol["source_freeze_health"])
    contract = _read(contract_path)
    health = _read(health_path)
    if contract["candidate_id"] != protocol["candidate_id"]:
        raise ValueError("contract candidate identity mismatch")
    if health["candidate_hash"] != contract["candidate_hash"]:
        raise ValueError("freeze health candidate identity mismatch")
    if not contract["signal_sequence_unchanged"]:
        raise ValueError("candidate unexpectedly changed the signal sequence")
    if int(contract["closed_trades"]) != 73:
        raise ValueError("candidate trade count differs from S001-v2")

    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "candidate_id": protocol["candidate_id"],
        "candidate_hash": contract["candidate_hash"],
        "primary_objective": protocol["primary_objective"],
        "signal_sequence_changed": False,
        "s001_v2_closed_trades": 73,
        "candidate_closed_trades": 73,
        "frequency_gain": 0.0,
        "calmar_change": float(contract["calmar"]) - 1.2424218499271011,
        "strategy_candidate_disposition": protocol["strategy_candidate_disposition"],
        "risk_baseline_disposition": protocol["risk_baseline_disposition"],
        "reason_codes": [
            "PRIMARY_OBJECTIVE_NOT_MET",
            "NO_SIGNAL_OR_FREQUENCY_CHANGE",
            "RISK_SCALING_ONLY",
        ],
        "evidence": {
            "contract_result_sha256": raw_file_sha256(contract_path),
            "freeze_health_sha256": raw_file_sha256(health_path),
        },
        "lifecycle_effects": {
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_deployed": False,
        },
    }
    _write(artifacts / "reclassification.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX12 执行\n\n状态：COMPLETE。已核对候选身份、信号序列与交易次数；"
        "旧实验及其证据未修改。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX12 结论\n\nS001-C001相对S001-v2的频率增益为0，"
        "不满足当前研究目标，降级为60%固定仓位风险基准，不冻结为S001-v3。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "candidate_id": protocol["candidate_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": protocol["strategy_candidate_disposition"],
            "risk_baseline_disposition": protocol["risk_baseline_disposition"],
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
