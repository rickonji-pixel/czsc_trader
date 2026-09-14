from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260914_S005_EX70"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_new_returns"):
        raise ValueError("EX70 may not read new returns")

    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/information_audit.json": source_spec["audit_sha256"],
        source / "artifacts/component_summary.csv": source_spec["summary_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")

    summary = pd.read_csv(source / "artifacts/component_summary.csv")
    rule = protocol["role_rule"]
    rows: list[dict[str, object]] = []
    for item in summary.to_dict("records"):
        top_positive = float(item["top_stress_mean"]) > 0
        residual_positive = float(item["residual_spearman_ic"]) > 0
        spread_probability = float(item["bootstrap_spread_positive_probability"])
        correlation = float(item["maximum_absolute_reference_correlation"])
        if (
            top_positive
            and residual_positive
            and spread_probability
            >= float(rule["minimum_modulator_spread_probability"])
            and correlation
            < float(rule["maximum_modulator_absolute_reference_correlation"])
        ):
            role = "CONFIDENCE_MODULATOR"
            reason = "positive absolute and residual value with strong spread evidence"
        elif not top_positive or not residual_positive:
            role = "AUDIT_LABEL_ONLY"
            reason = "absolute top-state return or residual information is non-positive"
        else:
            role = "RESEARCH_COMPONENT"
            reason = "directional evidence exists but is insufficient for a decision role"
        rows.append(
            {
                "factor_id": item["factor_id"],
                "ex69_label": item["label"],
                "role": role,
                "reason": reason,
                "frequency_policy": protocol["component_frequency_policy"],
            }
        )

    roles = pd.DataFrame(rows)
    industry_role = str(
        roles.loc[
            roles["factor_id"].eq("F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW"),
            "role",
        ].iloc[0]
    )
    decision = (
        "PROCEED_TO_OPPORTUNITY_LAYER_DISCOVERY_WITH_INDUSTRY_MODULATOR"
        if industry_role == "CONFIDENCE_MODULATOR"
        else "CONTINUE_OPPORTUNITY_LAYER_DISCOVERY_WITHOUT_NEW_MODULATOR"
    )
    roles.to_csv(artifacts / "component_roles.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "roles": dict(zip(roles["factor_id"], roles["role"], strict=True)),
        "decision": decision,
        "reads_new_returns": False,
        "complete_strategy_exists": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    (artifacts / "role_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX70 执行\n\n状态：`COMPLETE`。根据EX69已冻结证据确定组件职责，没有读取新收益。\n",
        encoding="utf-8",
    )
    role_text = "；".join(
        f"`{row.factor_id}`={row.role}" for row in roles.itertuples(index=False)
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX70 结论\n\n"
        f"裁决：`{decision}`。{role_text}。当前仍缺少高密度机会层，没有形成完整策略或候选。\n",
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
