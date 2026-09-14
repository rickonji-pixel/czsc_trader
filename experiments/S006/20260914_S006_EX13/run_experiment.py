from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX13"


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
    replay_path = source_experiment / "artifacts/discovery_only_replay_ledger.csv"
    panel_path = source_experiment / "artifacts/discovery_only_component_panel.csv"
    frozen = {
        source_experiment / "experiment_manifest.json": source["manifest_sha256"],
        replay_path: source["replay_ledger_sha256"],
        panel_path: source["component_panel_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")
    replay = pd.read_csv(replay_path)
    panel = pd.read_csv(panel_path)
    passed = replay.loc[replay["hard_gates_pass"]]
    if len(passed) != 1:
        raise ValueError("EX12 hard-gate result differs")
    winner = passed.iloc[0]
    weights = json.loads(str(winner["weights_json"]))
    nonzero_families = sorted(family for family, weight in weights.items() if float(weight) > 0)
    selected = panel.loc[panel["selected"] & panel["information_family"].isin(nonzero_families)].copy()
    top_n = int(protocol["complexity"]["components_per_nonzero_family_next_round"])
    compressed = selected.sort_values(["information_family", "oof_ic"], ascending=[True, False]).groupby("information_family", sort=True).head(top_n)
    compressed.to_csv(artifacts / "preregistered_compressed_panel.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    quantiles = sorted(replay["entry_quantile"].astype(float))
    winner_quantile = float(winner["entry_quantile"])
    position = quantiles.index(winner_quantile)
    adjacent = [value for index, value in enumerate(quantiles) if abs(index - position) == 1]
    adjacent_pass = int(replay.loc[replay["entry_quantile"].isin(adjacent), "hard_gates_pass"].sum())
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "tested_thresholds": int(len(replay)),
        "passing_thresholds": int(len(passed)),
        "adjacent_passing_thresholds": adjacent_pass,
        "parameter_platform": "NARROW",
        "current_components": int(winner["selected_components"]),
        "current_information_families": int(winner["information_families"]),
        "nonzero_information_families": len(nonzero_families),
        "compressed_components": int(len(compressed)),
        "compressed_information_families": int(compressed["information_family"].nunique()),
        "fixed_next_threshold_quantile": winner_quantile,
        "decision": "SIMPLIFY_BEFORE_CANDIDATE",
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False
    }
    _write(artifacts / "platform_complexity_audit.json", evidence)
    (experiment / "03_execution.md").write_text(f"# S006 EX13 执行\n\n状态：`COMPLETE`。6个阈值仅Q50通过，邻近阈值通过数为0；当前144个组件、11个信息族，非零权重10族。按发现期留一IC每族最多3个，下一轮面板压缩为{len(compressed)}个组件。未读取新收益。\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(f"# S006 EX13 结论\n\n裁决：`{evidence['decision']}`。EX12给出了有吸引力的数值，但参数平台为`NARROW`且144个组件不符合OPC实施成本。下一轮固定Q50，只复验预登记的{len(compressed)}组件、{evidence['compressed_information_families']}信息族压缩结构；通过前不得形成候选。\n", encoding="utf-8")
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": protocol["strategy_id"], "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": evidence["decision"], "promotion_allowed": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
