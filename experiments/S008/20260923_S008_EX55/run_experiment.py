from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX55"


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
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_development_returns",
        "reads_sealed_validation",
        "input_selection",
        "template_selection",
        "starts_search",
        "candidate_generation",
        "promotion_allowed",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("prototype preregistration permissions differ from frozen protocol")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
        "ex54_manifest_sha256": repo
        / "experiments/S008/20260923_S008_EX54/experiment_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")
    validate_experiment_archive(repo / "experiments/S008/20260923_S008_EX54")
    ex54 = _read(repo / "experiments/S008/20260923_S008_EX54/artifacts/role_review_summary.json")

    prototype = dict(protocol["prototype"])
    roles = dict(prototype["roles"])
    required_roles = {
        "opportunity": "gold_silver_ratio_distance_120",
        "confirmation": "gold_minus_silver_return_20",
    }
    checks = {
        "predecessor_allows_core_only_prototype": ex54.get("decision")
        == "PROCEED_TO_PRECIOUS_METAL_PREFERENCE_PROTOTYPE_WITHOUT_RISK_CONFIRMATION",
        "eligible_inputs_match": set(ex54.get("eligible_core_representatives", []))
        == set(required_roles.values()),
        "roles_match": roles == required_roles,
        "states_match": prototype.get("states") == ["FLAT", "LONG"],
        "positions_match": prototype.get("positions") == [0.0, 1.0],
        "risk_confirmation_forbidden": "rut_minus_spx_return_20"
        in protocol["forbidden_inputs"],
        "execution_is_confirmed": protocol["execution"]
        == {
            "decision_time": "China session T close",
            "fill_time": "China session T+1 open",
            "initial_cash": 1000000,
            "main_cost_one_way_bps": 10,
            "stress_cost_one_way_bps": 30,
        },
    }

    space = dict(protocol["search_space"])
    parameter_names = (
        "slow_entry",
        "slow_exit",
        "fast_entry",
        "fast_exit",
        "confirmation_sessions",
        "minimum_hold_sessions",
        "cooldown_sessions",
    )
    combinations = []
    for values in itertools.product(*(space[name] for name in parameter_names)):
        parameters = dict(zip(parameter_names, values, strict=True))
        if parameters["slow_exit"] < parameters["slow_entry"] and parameters["fast_exit"] < parameters["fast_entry"]:
            combinations.append(parameters)
    anchor = dict(protocol["implementation_anchor"])
    checks["search_space_has_valid_combinations"] = len(combinations) > 0
    checks["anchor_is_in_space"] = all(anchor[name] in space[name] for name in parameter_names)
    checks["anchor_obeys_hysteresis"] = (
        anchor["slow_exit"] < anchor["slow_entry"]
        and anchor["fast_exit"] < anchor["fast_entry"]
    )
    checks["hard_gate_count_is_two"] = len(protocol["evaluation"]["hard_gates"]) == 2
    decision = (
        protocol["adjudication"]["all_pass"]
        if all(checks.values())
        else protocol["adjudication"]["contract_failure"]
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "prototype_id": prototype["prototype_id"],
        "checks": checks,
        "valid_parameter_combination_count": len(combinations),
        "implementation_anchor": anchor,
        "reads_development_returns": False,
        "reads_sealed_validation": False,
        "search_started": False,
        "candidate_created": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "prototype_contract_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S008 EX55 执行记录\n\n"
        f"完成原型职责、状态、因果、执行和参数边界检查；合同检查通过="
        f"{sum(bool(value) for value in checks.values())}/{len(checks)}，滞回约束后的参数组合数="
        f"{len(combinations)}。没有读取收益、启动搜索或创建候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX55 结论\n\n"
        f"机器裁决：`{decision}`。原型只允许120日金银比慢速机会源和20日金银相对收益快速确认，"
        "禁止使用EX54拒绝的全球股市风险确认。通过只授予实现与合成可表达性检查资格。\n",
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
