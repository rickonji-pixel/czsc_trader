from __future__ import annotations

from dataclasses import asdict
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
    stationary_bootstrap_performance,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260912_S004_EX15"
SELECTED_ID = "S004-C001"


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


def _daily_path(
    frame: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    *,
    date_column: str,
    return_column: str,
) -> pd.Series:
    selected = frame[[date_column, return_column]].copy()
    selected[date_column] = pd.to_datetime(selected[date_column]).dt.normalize()
    grouped = selected.groupby(date_column, observed=True)[return_column].apply(
        lambda values: float(np.prod(1.0 + values.astype(float).to_numpy()) - 1.0)
    )
    result = pd.Series(0.0, index=calendar, dtype=float)
    missing = grouped.index.difference(calendar)
    if not missing.empty:
        raise ValueError(f"trial return dates fall outside the audit calendar: {missing[:3].tolist()}")
    result.loc[grouped.index] = grouped.to_numpy()
    return result


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
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("automatic_acceptance", "parameter_selection", "candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(bool(protocol.get(key)) for key in forbidden):
        raise ValueError("statistical audit cannot select, promote or deploy")
    source_ids = (
        "20260912_S004_EX04",
        "20260912_S004_EX07",
        "20260912_S004_EX09",
        "20260912_S004_EX12",
        "20260912_S004_EX14",
    )
    sources = {item: repo / "experiments" / "S004" / item for item in source_ids}
    for source in sources.values():
        validate_experiment_archive(source)

    dataset = protocol["dataset"]
    context = RepositoryContext.discover(repo)
    data = load_replay_data(
        context,
        "research",
        "588080.SH",
        "etf",
        pd.Timestamp(dataset["development_cutoff"]).date(),
    )
    sessions = pd.DatetimeIndex(pd.to_datetime(data.execution_daily["dt"])).normalize()
    calendar = sessions[
        (sessions >= pd.Timestamp(dataset["evaluation_start"]))
        & (sessions <= pd.Timestamp(dataset["development_cutoff"]))
    ]
    paths: dict[str, pd.Series] = {}
    ledger: list[dict[str, object]] = []

    inputs = (
        ("EX04", sources["20260912_S004_EX04"] / "artifacts/episodes.csv.gz", "mechanism_id", "exit_date", "stress_return"),
        ("EX07", sources["20260912_S004_EX07"] / "artifacts/platform_episodes.csv.gz", "cell_id", "entry_date", "stress_return"),
        ("EX09", sources["20260912_S004_EX09"] / "artifacts/proxy_episodes.csv.gz", "mechanism_id", "exit_date", "stress_return"),
    )
    for prefix, path, id_column, date_column, return_column in inputs:
        frame = pd.read_csv(path)
        for trial_id, group in frame.groupby(id_column, sort=True, observed=True):
            name = f"{prefix}:{trial_id}"
            paths[name] = _daily_path(
                group,
                calendar,
                date_column=date_column,
                return_column=return_column,
            )
            ledger.append({"trial_id": name, "source": str(path.relative_to(repo)).replace("\\", "/"), "episodes": int(len(group))})
    selected = pd.read_csv(sources["20260912_S004_EX12"] / "artifacts/candidate_episodes.csv.gz")
    paths[SELECTED_ID] = _daily_path(
        selected,
        calendar,
        date_column="exit_date",
        return_column="stress_return",
    )
    ledger.append({"trial_id": SELECTED_ID, "source": "experiments/S004/20260912_S004_EX12/artifacts/candidate_episodes.csv.gz", "episodes": int(len(selected))})
    matrix = pd.DataFrame(paths, index=calendar)
    expected_count = int(protocol["trial_universe"]["raw_trial_count"])
    if matrix.shape != (len(calendar), expected_count) or len(ledger) != expected_count:
        raise AssertionError(f"trial matrix shape differs: {matrix.shape}")
    if matrix.std().le(0).any():
        raise AssertionError("one or more trial paths have no dispersion")

    evidence = ReturnMatrixEvidence(
        tuple(calendar.strftime("%Y-%m-%d")),
        tuple(matrix.columns),
        tuple(tuple(float(value) for value in row) for row in matrix.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates,
        evidence.candidate_ids,
        evidence.returns,
        hash_return_matrix(evidence),
    )
    audit_spec = protocol["audit"]
    pbo = cscv_pbo(evidence, int(audit_spec["cscv_blocks"]))
    sharpes = np.asarray([annualized_sharpe(matrix[column].to_numpy()) for column in matrix])
    effective_count = effective_trial_count(matrix.to_numpy())
    dsr = calculate_dsr_bundle(
        matrix[SELECTED_ID].to_numpy(),
        sharpes,
        raw_count=expected_count,
        effective_count=effective_count,
    )
    bootstraps = [
        stationary_bootstrap_performance(
            matrix[SELECTED_ID].to_numpy(),
            candidate_id=SELECTED_ID,
            repetitions=int(audit_spec["bootstrap_repetitions"]),
            mean_block_length=int(block_length),
            seed=int(audit_spec["seed"]) + int(block_length),
        )
        for block_length in audit_spec["bootstrap_mean_block_lengths"]
    ]
    primary = next(item for item in bootstraps if item.mean_block_length == 21)
    labels = {
        "pbo": _label(float(pbo.pbo), float(audit_spec["favorable_pbo_max"]), float(audit_spec["adverse_pbo_min_exclusive"]), lower_better=True),
        "dsr": _label(float(dsr.effective.probability), float(audit_spec["favorable_dsr_probability_min"]), float(audit_spec["adverse_dsr_probability_below"]), lower_better=False),
        "absolute_bootstrap": "FAVORABLE" if primary.cagr.lower_90 > 0 else "MIXED" if primary.cagr.upper_90 > 0 else "ADVERSE",
    }
    overall = "ADVERSE" if "ADVERSE" in labels.values() else "FAVORABLE" if set(labels.values()) == {"FAVORABLE"} else "MIXED"

    matrix.reset_index(names="date").to_csv(artifacts / "daily_return_matrix.csv.gz", index=False, compression={"method": "gzip", "compresslevel": 9, "mtime": 0})
    pd.DataFrame(ledger).to_csv(artifacts / "trial_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.DataFrame({"trial_id": matrix.columns, "annualized_sharpe": sharpes}).to_csv(artifacts / "trial_sharpes.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write(artifacts / "statistical_details.json", {
        "pbo": pbo.to_dict(),
        "dsr": {"raw": asdict(dsr.raw), "effective": asdict(dsr.effective)},
        "absolute_bootstrap": [item.to_dict() for item in bootstraps],
    })
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": overall,
        "direction_labels": labels,
        "raw_trial_count": expected_count,
        "effective_trial_count": effective_count,
        "pbo": pbo.pbo,
        "dsr_raw_probability": dsr.raw.probability,
        "dsr_effective_probability": dsr.effective.probability,
        "bootstrap_21d_cagr_lower_90": primary.cagr.lower_90,
        "bootstrap_21d_calmar_lower_90": primary.calmar.lower_90,
        "candidate_created": False,
    }
    _write(artifacts / "statistical_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# 20260912_S004_EX15 执行\n\n状态：COMPLETE。SE已对{expected_count}条历史路径计算PBO、有效试验数、DSR和三档区块Bootstrap。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX15 结论\n\n"
        f"总标签：`{overall}`。PBO {pbo.pbo:.2%}；有效试验数 {effective_count:.2f}；"
        f"有效DSR概率 {dsr.effective.probability:.2%}；21日区块Bootstrap年化收益90%下界 {primary.cagr.lower_90:.2%}，卡玛90%下界 {primary.calmar.lower_90:.2f}。\n\n"
        "本轮只报告统计证据，不改变候选、SM或PTE状态。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S004",
        "symbol": "588080.SH",
        "candidate_id": SELECTED_ID,
        "development_cutoff": dataset["development_cutoff"],
        "evidence_label": overall,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
