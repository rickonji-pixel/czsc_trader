from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX11"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    source = protocol["source"]
    source_experiment = repo / "experiments/S006" / str(source["experiment_id"])
    validate_experiment_archive(source_experiment)
    ledger_path = source_experiment / "artifacts/architecture_search_ledger.csv"
    if _sha256(source_experiment / "experiment_manifest.json") != str(source["manifest_sha256"]):
        raise ValueError("EX10 manifest differs")
    if _sha256(ledger_path) != str(source["ledger_sha256"]):
        raise ValueError("EX10 ledger differs")
    if protocol.get("reads_new_returns"):
        raise ValueError("EX11 must not read new returns")

    ledger = pd.read_csv(ledger_path)
    qualified = ledger.loc[ledger["hard_gates_pass"]].copy()
    if len(qualified) != 2 or not qualified["panel"].eq("STABLE").all():
        raise ValueError("qualified architecture facts differ from EX10")
    qualified["minimum_split_cagr"] = qualified[["discovery_cagr", "confirmation_cagr"]].min(axis=1)
    qualified["selection_bias_status"] = "CONFIRMATION_USED_IN_PANEL_SELECTION"
    qualified = qualified.sort_values(["minimum_split_cagr", "full_maximum_drawdown", "full_rolling_60_closed_trades_median"], ascending=[False, False, False])
    qualified.to_csv(artifacts / "qualified_architecture_review.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    reference = qualified.iloc[0]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "qualified_paths_reviewed": int(len(qualified)),
        "confirmation_independent_paths": 0,
        "reference_architecture_path": str(reference["path_id"]),
        "reference_discovery_cagr": float(reference["discovery_cagr"]),
        "reference_confirmation_cagr": float(reference["confirmation_cagr"]),
        "reference_full_maximum_drawdown": float(reference["full_maximum_drawdown"]),
        "reference_frequency_median": float(reference["full_rolling_60_closed_trades_median"]),
        "reference_frequency_p10": float(reference["full_rolling_60_closed_trades_p10"]),
        "decision": "REBUILD_COMPONENT_PANEL_FROM_DISCOVERY_ONLY",
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False
    }
    _write(artifacts / "selection_bias_review.json", evidence)
    (experiment / "03_execution.md").write_text("# S006 EX11 执行\n\n状态：`COMPLETE`。复核EX10两条合格路径，确认二者均使用含确认期方向标签的`STABLE`组件面板，因此确认期表现不能视为独立外推。未读取新收益。\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(f"# S006 EX11 结论\n\n裁决：`{evidence['decision']}`。保留`{evidence['reference_architecture_path']}`作为结构参考：发现期年化{evidence['reference_discovery_cagr']:.2%}、确认期年化{evidence['reference_confirmation_cagr']:.2%}、完整回撤{evidence['reference_full_maximum_drawdown']:.2%}、频率{evidence['reference_frequency_median']:.1f}/{evidence['reference_frequency_p10']:.1f}。下一轮必须只用发现期建立组件面板，再由确认期检验；当前不形成候选。\n", encoding="utf-8")
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": protocol["strategy_id"], "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": evidence["decision"], "promotion_allowed": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
