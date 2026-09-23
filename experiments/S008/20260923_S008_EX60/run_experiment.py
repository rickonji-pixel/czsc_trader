from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX60"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from protocol")
    sources = {
        "ex59_manifest_sha256": repo / "experiments/S008/20260923_S008_EX59/experiment_manifest.json",
        "ex55_protocol_sha256": repo / "experiments/S008/20260923_S008_EX55/artifacts/protocol.json",
        "materials_sha256": repo / "research/S008/materials.json",
    }
    for key, path in sources.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")
    validate_experiment_archive(repo / "experiments/S008/20260923_S008_EX59")
    if any(
        protocol.get(key)
        for key in ("reads_real_returns", "starts_search", "selects_winner", "candidate_generation")
    ):
        raise ValueError("preregistration cannot read returns, search, select or promote")

    space = dict(protocol["search_space"])
    names = list(space)
    valid = 0
    for values in itertools.product(*(space[name] for name in names)):
        row = dict(zip(names, values, strict=True))
        if row["slow_exit"] < row["slow_entry"] and row["fast_exit"] < row["fast_entry"]:
            valid += 1
    if valid != int(protocol["valid_parameter_combination_count"]):
        raise ValueError("valid parameter count differs from frozen contract")
    search = dict(protocol["search_execution"])
    if (
        search["storage"] != "optuna.storages.InMemoryStorage"
        or int(search["logical_workers"]) != 8
        or int(search["trial_budget"]) != 1500
        or bool(search["resume"])
        or bool(search["early_stopping"])
        or bool(search["pruning"])
    ):
        raise ValueError("search execution differs from confirmed contract")
    if len(protocol["evaluation_contract"]["hard_gates"]) != 2:
        raise ValueError("only the two confirmed hard gates are allowed")

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "PRECIOUS_METAL_PREFERENCE_SEARCH_CONTRACT_FROZEN",
        "prototype_id": protocol["prototype_id"],
        "valid_parameter_combination_count": valid,
        "trial_budget": search["trial_budget"],
        "logical_workers": search["logical_workers"],
        "storage": "InMemoryStorage",
        "hard_gate_count": 2,
        "reads_real_returns": False,
        "search_started": False,
        "winner_selected": False,
        "candidate_created": False,
    }
    _write(artifacts / "search_contract_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX60 执行记录\n\n"
        "已验证EX59完整归档、EX55参数源和S008材料哈希；5,940个合法组合、1,500次NSGA-II预算、"
        "InMemoryStorage、8逻辑核分批评价、两项硬门和禁止事项均闭合。未读取真实收益或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX60 结论\n\n"
        "机器裁决：`PRECIOUS_METAL_PREFERENCE_SEARCH_CONTRACT_FROZEN`。本结论只授权按冻结合同创建"
        "正式搜索实验，不提供Alpha、参数优劣或候选证据。\n",
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
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
