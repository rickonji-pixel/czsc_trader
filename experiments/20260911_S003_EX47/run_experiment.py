from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import (
    ReturnMatrixEvidence,
    annualized_sharpe,
    calculate_dsr_bundle,
    cscv_pbo,
    effective_trial_count,
    hash_return_matrix,
)

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_opportunity_map import _price_checkpoints


EXPERIMENT_ID = "20260911_S003_EX47"


def _read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_ex46_helpers(path: Path) -> object:
    spec = importlib.util.spec_from_file_location("s003_ex46_frozen", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load frozen EX46 helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _label(value: float, favorable: float, adverse: float, *, lower_better: bool) -> str:
    if lower_better:
        if value <= favorable:
            return "FAVORABLE"
        if value > adverse:
            return "ADVERSE"
    else:
        if value >= favorable:
            return "FAVORABLE"
        if value < adverse:
            return "ADVERSE"
    return "MIXED"


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "automatic_acceptance",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX47 may only correct statistical evidence")

    source_spec = protocol["source"]
    source = repo / "experiments" / source_spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "run_experiment.py": source_spec["run_script_sha256"],
        source / "artifacts/statistical_summary.json": source_spec[
            "statistical_summary_sha256"
        ],
        source / "artifacts/trial_sources.json": source_spec["trial_sources_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"EX46 source differs: {path.name}")

    correction = protocol["correction"]
    invalid = repo / "experiments" / correction["invalid_experiment_id"]
    invalid_manifest = _read(invalid / "experiment_manifest.json")
    if invalid_manifest.get("status") != correction["invalid_manifest_status"]:
        raise ValueError("excluded experiment is not marked INVALID")

    ex46_protocol = _read(source / "artifacts/protocol.json")
    inventory = _read(source / "artifacts/trial_sources.json")
    helpers = _load_ex46_helpers(source / "run_experiment.py")
    intraday = load_intraday_research_data(repo / "data/raw", "510500.SH")
    checkpoints = _price_checkpoints(intraday.frames["5m"])
    start = pd.Timestamp(ex46_protocol["dataset"]["evaluation_start"])
    end = pd.Timestamp(ex46_protocol["dataset"]["development_cutoff"])
    calendar = checkpoints.loc[(checkpoints.index >= start) & (checkpoints.index <= end)].index
    fraction = float(ex46_protocol["trial_universe"]["daily_capital_fraction"])
    full_matrix, ledger = helpers._trial_matrix(repo, inventory, calendar, fraction)

    attempted = int(correction["attempted_trial_count"])
    if len(full_matrix.columns) != attempted or len(ledger) != attempted:
        raise ValueError("attempted trial universe differs from frozen correction")
    invalid_prefix = f"{correction['invalid_experiment_id']}:"
    invalid_columns = [column for column in full_matrix if column.startswith(invalid_prefix)]
    if len(invalid_columns) != int(correction["excluded_invalid_return_paths"]):
        raise ValueError("invalid return path count differs from frozen correction")
    matrix = full_matrix.drop(columns=invalid_columns)
    comparable = int(correction["comparable_return_path_count"])
    if len(matrix.columns) != comparable:
        raise ValueError("comparable return path count differs from frozen correction")
    ledger["eligible_return_evidence"] = ~ledger["trial_id"].str.startswith(invalid_prefix)
    ledger["return_evidence_reason"] = np.where(
        ledger["eligible_return_evidence"], "VALID_EXPERIMENT", correction["invalid_reason"]
    )

    selected_id = (
        "20260911_S003_EX45:CONSTITUENT_MONEYFLOW_BREADTH_CONTINUATION:PRIMARY_LONG:1"
    )
    evidence = ReturnMatrixEvidence(
        tuple(date.strftime("%Y-%m-%d") for date in calendar),
        tuple(map(str, matrix.columns)),
        tuple(tuple(float(value) for value in row) for row in matrix.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates, evidence.candidate_ids, evidence.returns, hash_return_matrix(evidence)
    )
    audit = protocol["audit"]
    pbo = cscv_pbo(evidence, int(audit["cscv_blocks"]))
    sharpes = np.asarray([annualized_sharpe(matrix[column].to_numpy()) for column in matrix])
    effective_count = effective_trial_count(matrix.to_numpy())
    dsr = calculate_dsr_bundle(
        matrix[selected_id].to_numpy(),
        sharpes,
        raw_count=int(correction["dsr_raw_trial_count"]),
        effective_count=effective_count,
    )

    ex46_summary = _read(source / "artifacts/statistical_summary.json")
    direction_flags = dict(ex46_summary["direction_flags"])
    direction_flags["pbo"] = _label(
        pbo.pbo,
        float(audit["favorable_pbo_max"]),
        float(audit["adverse_pbo_min_exclusive"]),
        lower_better=True,
    )
    direction_flags["dsr"] = _label(
        dsr.effective.probability,
        float(audit["positive_dsr_probability"]),
        float(audit["adverse_dsr_probability_below"]),
        lower_better=False,
    )
    if "ADVERSE" in direction_flags.values():
        overall = "ADVERSE"
    elif set(direction_flags.values()) == {"FAVORABLE"}:
        overall = "FAVORABLE"
    else:
        overall = "MIXED"

    ledger.to_csv(
        artifacts / "corrected_trial_ledger.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    pd.DataFrame({"trial_id": matrix.columns, "annualized_sharpe": sharpes}).to_csv(
        artifacts / "comparable_trial_sharpes.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    pd.DataFrame([asdict(item) for item in pbo.splits]).to_csv(
        artifacts / "corrected_cscv_splits.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": overall,
        "direction_flags": direction_flags,
        "attempted_trial_count": attempted,
        "comparable_return_path_count": comparable,
        "excluded_invalid_return_paths": len(invalid_columns),
        "effective_trial_count": effective_count,
        "pbo": pbo.pbo,
        "dsr_raw_probability": dsr.raw.probability,
        "dsr_effective_probability": dsr.effective.probability,
        "candidate_created": False,
    }
    _write(artifacts / "corrected_statistical_summary.json", summary)
    _write(
        artifacts / "corrected_statistical_details.json",
        {
            "return_matrix_hash": evidence.content_hash,
            "pbo": pbo.to_dict(),
            "dsr": {"raw": asdict(dsr.raw), "effective": asdict(dsr.effective)},
        },
    )
    flag_table = ["|审计方向|标签|", "|---|---|"] + [
        f"|{name}|`{label}`|" for name, label in direction_flags.items()
    ]
    (experiment / "03_execution.md").write_text(
        "# S003 EX47 执行\n\n"
        f"状态：COMPLETE。保留{attempted}次历史尝试的原始搜索惩罚，从经验收益分布剔除"
        f"{len(invalid_columns)}条已知无效路径，以{comparable}条可比较路径重算PBO和DSR。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX47 结论\n\n"
        f"修正后总标签：`{overall}`。PBO为{pbo.pbo:.2%}，有效试验数{effective_count:.2f}，"
        f"有效DSR概率{dsr.effective.probability:.2%}。\n\n"
        + "\n".join(flag_table)
        + "\n\n"
        + (
            "存在ADVERSE证据，停止该原型，不创建候选。"
            if overall == "ADVERSE"
            else "没有触发停止条件，可进入研究候选登记评审。"
        )
        + " 本实验没有修改SM或PTE。\n",
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
            "development_cutoff": ex46_protocol["dataset"]["development_cutoff"],
            "evidence_label": overall,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
