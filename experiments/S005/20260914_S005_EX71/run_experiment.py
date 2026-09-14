from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import time

import pandas as pd

from factor_signal_catalog import CatalogRegistry
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX71"


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


def _classification(row: pd.Series) -> str:
    if not bool(row["check_coverage"]):
        return "DATA_COVERAGE_FAIL"
    if not bool(row["canonical_state"]):
        return "DUPLICATE_ALIAS"
    if not bool(row["check_semantic"]):
        return "SEMANTIC_CONTEXT_ONLY"
    return "INFORMATION_AUDIT_UNIVERSE"


def _restoration_reason(row: pd.Series) -> str:
    reasons: list[str] = []
    if not bool(row["check_active_ratio"]):
        reasons.append("OLD_ACTIVE_RATIO_RULE")
    if not bool(row["check_transitions"]):
        reasons.append("OLD_TRANSITION_COUNT_RULE")
    if not bool(row["check_years"]):
        reasons.append("OLD_COVERAGE_YEARS_RULE")
    return "|".join(reasons)


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    forbidden = (
        "reads_post_signal_prices",
        "candidate_generation",
        "parameter_search",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if protocol.get("experiment_id") != EXPERIMENT_ID or any(protocol.get(key) for key in forbidden):
        raise ValueError("EX71 protocol only permits return-free eligibility reclassification")

    source_spec = protocol["source"]
    source = repo / "experiments" / "S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    for key in ("state_eligibility", "primary_states", "signal_catalog"):
        path = source / str(source_spec[f"{key}_path"])
        if _sha256(path) != str(source_spec[f"{key}_sha256"]):
            raise ValueError(f"EX49 {key} evidence differs from the frozen EX71 protocol")

    catalog = CatalogRegistry(repo / "catalog")
    if catalog.digest != str(protocol["catalog_digest"]):
        raise ValueError("FSC differs from the frozen EX71 protocol")
    definitions = {item.name: item for item in catalog.signals if item.provider == "czsc"}

    source_path = source / str(source_spec["state_eligibility_path"])
    states = pd.read_csv(source_path)
    if len(states) != 2092:
        raise ValueError(f"expected 2092 generated states, got {len(states)}")
    missing = sorted(set(map(str, states["name"])) - set(definitions))
    if missing:
        raise ValueError(f"generated CZSC functions missing from FSC: {missing[:3]}")

    states["catalog_signal_id"] = states["name"].map(lambda value: definitions[str(value)].signal_id)
    states["information_family"] = states["name"].map(
        lambda value: definitions[str(value)].information_family
    )
    states["evaluation_status_v2"] = states.apply(_classification, axis=1)
    states["technical_eligible_v2"] = states["evaluation_status_v2"].eq(
        "INFORMATION_AUDIT_UNIVERSE"
    )
    states["frequency_policy"] = "REPORT_ONLY"
    states["restored_from_old_filter"] = (
        states["technical_eligible_v2"] & ~states["technical_eligible"].astype(bool)
    )
    states["restoration_reason"] = states.apply(_restoration_reason, axis=1)
    states.loc[~states["restored_from_old_filter"], "restoration_reason"] = ""
    states = states.sort_values(
        ["evaluation_status_v2", "information_family", "frequency", "name", "state_primary"]
    ).reset_index(drop=True)

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    states.to_csv(
        artifacts / "reclassified_state_ledger.csv.gz",
        index=False,
        encoding="utf-8",
        compression=compression,
        lineterminator="\n",
    )

    eligible = states.loc[states["technical_eligible_v2"]].copy()
    family = (
        eligible.groupby(["information_family", "frequency"], as_index=False)
        .agg(
            state_count=("state_id", "size"),
            signal_configurations=("signal_id", "nunique"),
            restored_states=("restored_from_old_filter", "sum"),
            active_ratio_median=("active_ratio", "median"),
            transition_median=("independent_transitions", "median"),
            rolling_60_median_observed=("rolling_60_median", "median"),
            rolling_60_p10_observed=("rolling_60_p10", "median"),
        )
        .sort_values(["information_family", "frequency"])
    )
    family.to_csv(
        artifacts / "family_inventory.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )

    restored = states.loc[states["restored_from_old_filter"]].copy()
    reason_counts = Counter()
    for value in restored["restoration_reason"]:
        reason_counts.update(str(value).split("|"))
    reasons = pd.DataFrame(
        [{"old_rule": key, "restored_state_count": value} for key, value in sorted(reason_counts.items())]
    )
    reasons.to_csv(
        artifacts / "restoration_reasons.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )

    status_counts = states["evaluation_status_v2"].value_counts().sort_index()
    frequency_counts = eligible["frequency"].value_counts().sort_index()
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "source_state_count": int(len(states)),
        "old_technical_eligible_states": int(states["technical_eligible"].sum()),
        "information_audit_universe_states": int(len(eligible)),
        "restored_states": int(len(restored)),
        "evaluation_status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "audit_universe_frequency_counts": {
            str(key): int(value) for key, value in frequency_counts.items()
        },
        "restoration_reason_counts": dict(sorted(reason_counts.items())),
        "information_family_count": int(eligible["information_family"].nunique()),
        "all_states_accounted_for": int(status_counts.sum()) == int(len(states)),
        "frequency_policy": "REPORT_ONLY_AT_COMPONENT_STAGE",
        "complete_strategy_frequency_gate": protocol["complete_strategy_frequency_gate"],
        "reads_post_signal_prices": False,
        "decision": "PROCEED_TO_ALL_STATE_INFORMATION_AUDIT_DESIGN",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "census_correction.json", evidence)

    (experiment / "03_execution.md").write_text(
        "# S005 EX71 执行\n\n"
        f"状态：`COMPLETE`。EX49的{len(states)}个已生成状态全部完成新口径归类。旧技术池"
        f"{evidence['old_technical_eligible_states']}项；新信息审计全集"
        f"{evidence['information_audit_universe_states']}项，恢复"
        f"{evidence['restored_states']}项。组件频率只记录，本轮没有读取收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX71 结论\n\n"
        "裁决：`PROCEED_TO_ALL_STATE_INFORMATION_AUDIT_DESIGN`。S005的CZSC信息审计全集由"
        f"旧口径950项扩大到{len(eligible)}项，恢复{len(restored)}项。新全集按频率为："
        + "、".join(f"{key} {value}项" for key, value in frequency_counts.items())
        + "。另有"
        f"{int(status_counts.get('SEMANTIC_CONTEXT_ONLY', 0))}项语义上下文和"
        f"{int(status_counts.get('DUPLICATE_ALIAS', 0))}项精确重复别名继续保留在总账中。\n\n"
        "恢复仅表示获得信息价值审计资格，不代表具有Alpha、方向性或适合充当机会层。下一轮须在"
        "1525项完整全集上预注册方向、期限、去冗余、稳定性和互补性口径；任何收益读取均计入搜索"
        "惩罚。滚动60日`7/3`门只在完整策略形成后执行。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["research_target"]["strategy_id"],
            "symbol": protocol["research_target"]["symbol"],
            "development_cutoff": protocol["dataset"]["cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
