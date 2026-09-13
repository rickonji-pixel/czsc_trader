from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENT_ID = "20260913_S005_EX48"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _coherence(
    returns: pd.DataFrame,
    active_by_date: dict[pd.Timestamp, list[str]],
    window: int,
    minimum: int,
) -> pd.Series:
    values: list[float] = []
    for position, date in enumerate(returns.index):
        if position + 1 < window:
            values.append(float("nan"))
            continue
        active = [code for code in active_by_date[date] if code in returns.columns]
        frame = returns.iloc[position - window + 1 : position + 1][active]
        corr = frame.corr(min_periods=minimum).to_numpy(dtype=float)
        upper = corr[np.triu_indices_from(corr, k=1)]
        upper = upper[np.isfinite(upper)]
        values.append(float(upper.mean()) if upper.size else float("nan"))
    return pd.Series(values, index=returns.index, name="coherence_20")


def _state_cycles(frame: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    active = False
    entries: list[pd.Timestamp] = []
    exits: list[pd.Timestamp] = []
    states: list[bool] = []
    for date, row in frame.iterrows():
        ready = bool(row[[
            "breadth_entry", "coherence_entry", "leadership_entry",
            "breadth_exit", "coherence_exit", "leadership_exit",
        ]].notna().all())
        enter = ready and (
            row["breadth_persistence_3"] >= row["breadth_entry"]
            and row["coherence_20"] >= row["coherence_entry"]
            and row["leadership_concentration"] <= row["leadership_entry"]
        )
        leave = ready and (
            row["breadth_persistence_3"] <= row["breadth_exit"]
            or row["coherence_20"] <= row["coherence_exit"]
            or row["leadership_concentration"] >= row["leadership_exit"]
        )
        if not active and enter:
            active = True
            entries.append(date)
        elif active and leave:
            active = False
            exits.append(date)
        states.append(active)
    if len(entries) > len(exits):
        exits.append(pd.NaT)
    cycles = pd.DataFrame({"entry_date": entries, "exit_date": exits})
    positions = {date: pos for pos, date in enumerate(frame.index)}
    cycles["holding_sessions"] = [
        (positions[exit_date] - positions[entry_date] + 1) if pd.notna(exit_date) else np.nan
        for entry_date, exit_date in zip(cycles["entry_date"], cycles["exit_date"], strict=True)
    ]
    return pd.Series(states, index=frame.index, name="state_active"), cycles


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    sys.path.insert(0, str(repo / "src"))
    from czsc_trader.experiment_archive import (  # noqa: PLC0415
        build_experiment_manifest,
        validate_experiment_archive,
    )
    from czsc_trader.identity import raw_file_sha256  # noqa: PLC0415

    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source = protocol["source"]
    panel_dir = repo / "experiments/S005" / str(source["panel_experiment"])
    review_dir = repo / "experiments/S005" / str(source["review_experiment"])
    validate_experiment_archive(panel_dir)
    validate_experiment_archive(review_dir)
    checks = (
        (panel_dir / "experiment_manifest.json", source["panel_manifest_sha256"]),
        (panel_dir / "artifacts/constituent_panel.csv.gz", source["panel_sha256"]),
        (review_dir / "experiment_manifest.json", source["review_manifest_sha256"]),
        (review_dir / "artifacts/reviewed_data_quality.json", source["review_quality_sha256"]),
    )
    for path, expected in checks:
        if raw_file_sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")
    if not _read(review_dir / "artifacts/reviewed_data_quality.json")["passed"]:
        raise ValueError("reviewed constituent data gate did not pass")

    panel = pd.read_csv(panel_dir / "artifacts/constituent_panel.csv.gz", parse_dates=["dt"])
    panel["dt"] = panel["dt"].dt.normalize()
    panel = panel[panel["observed_daily"]].copy()
    panel["weight"] = pd.to_numeric(panel["weight"], errors="raise")
    panel["pct_chg"] = pd.to_numeric(panel["pct_chg"], errors="raise") / 100.0
    panel["positive_weight"] = panel["weight"].where(panel["pct_chg"].gt(0), 0.0)
    panel["abs_contribution"] = panel["weight"] * panel["pct_chg"].abs()
    daily = panel.groupby("dt", sort=True, observed=True).agg(
        positive_weight=("positive_weight", "sum"),
        observed_weight=("weight", "sum"),
        observed_members=("con_code", "nunique"),
    )
    daily["price_breadth"] = daily["positive_weight"] / daily["observed_weight"]
    feature = protocol["feature"]
    breadth_window = int(feature["breadth_persistence_sessions"])
    daily["breadth_persistence_3"] = daily["price_breadth"].rolling(
        breadth_window, min_periods=breadth_window
    ).mean()

    contributions = panel.sort_values(["dt", "abs_contribution"], ascending=[True, False])
    top = contributions.groupby("dt", observed=True).head(int(feature["leadership_top_members"]))
    total_contribution = panel.groupby("dt", observed=True)["abs_contribution"].sum()
    top_contribution = top.groupby("dt", observed=True)["abs_contribution"].sum()
    daily["leadership_concentration"] = top_contribution / total_contribution.replace(0.0, np.nan)

    returns = panel.pivot(index="dt", columns="con_code", values="pct_chg").sort_index()
    active_by_date = panel.groupby("dt", observed=True)["con_code"].apply(list).to_dict()
    daily["coherence_20"] = _coherence(
        returns,
        active_by_date,
        int(feature["coherence_sessions"]),
        int(feature["coherence_minimum_pair_observations"]),
    )

    reference = int(feature["reference_sessions"])
    minimum = int(feature["reference_minimum_observations"])
    shifted = daily[["breadth_persistence_3", "coherence_20", "leadership_concentration"]].shift(1)
    daily["breadth_entry"] = shifted["breadth_persistence_3"].rolling(reference, min_periods=minimum).quantile(float(feature["entry_quantile"]))
    daily["breadth_exit"] = shifted["breadth_persistence_3"].rolling(reference, min_periods=minimum).quantile(float(feature["exit_quantile"]))
    daily["coherence_entry"] = shifted["coherence_20"].rolling(reference, min_periods=minimum).quantile(float(feature["neutral_quantile"]))
    daily["coherence_exit"] = shifted["coherence_20"].rolling(reference, min_periods=minimum).quantile(float(feature["exit_quantile"]))
    daily["leadership_entry"] = shifted["leadership_concentration"].rolling(reference, min_periods=minimum).quantile(float(feature["neutral_quantile"]))
    daily["leadership_exit"] = shifted["leadership_concentration"].rolling(reference, min_periods=minimum).quantile(float(feature["entry_quantile"]))

    state, cycles = _state_cycles(daily)
    daily["state_active"] = state
    entry_flags = pd.Series(False, index=daily.index)
    entry_flags.loc[pd.DatetimeIndex(cycles["entry_date"])] = True
    ready_date = daily["breadth_entry"].first_valid_index()
    density = protocol["density"]
    rolling = entry_flags.loc[entry_flags.index >= ready_date].astype(int).rolling(
        int(density["rolling_window_sessions"]),
        min_periods=int(density["rolling_window_sessions"]),
    ).sum().dropna()
    complete = cycles[cycles["exit_date"].notna()].copy()
    annual = cycles.groupby(pd.DatetimeIndex(cycles["entry_date"]).year).size()
    metrics = {
        "feature_sessions": int(len(daily)),
        "first_ready_session": ready_date.date().isoformat(),
        "independent_cycles": int(len(cycles)),
        "complete_cycles": int(len(complete)),
        "rolling_60_median": float(rolling.median()),
        "rolling_60_p10": float(rolling.quantile(0.1)),
        "rolling_60_minimum": float(rolling.min()),
        "rolling_60_maximum": float(rolling.max()),
        "state_occupancy": float(daily.loc[daily.index >= ready_date, "state_active"].mean()),
        "median_holding_sessions": float(complete["holding_sessions"].median()),
        "p90_holding_sessions": float(complete["holding_sessions"].quantile(0.9)),
        "annual_cycle_counts": {str(int(year)): int(count) for year, count in annual.items()},
    }
    density_pass = bool(
        metrics["rolling_60_median"] >= float(density["minimum_median"])
        and metrics["rolling_60_p10"] >= float(density["minimum_p10"])
    )
    decision = "PROCEED_TO_FROZEN_STATE_RETURN_TEST" if density_pass else "STOP_COHERENCE_STATE_ON_DENSITY"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "mechanism": "BROAD_COHERENCE_STATE_V1",
        "metrics": metrics,
        "checks": {
            "rolling_60_median": metrics["rolling_60_median"] >= float(density["minimum_median"]),
            "rolling_60_p10": metrics["rolling_60_p10"] >= float(density["minimum_p10"]),
        },
        "decision": decision,
        "reads_post_state_prices": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    out = daily.reset_index()
    out["dt"] = out["dt"].dt.strftime("%Y-%m-%d")
    out.to_csv(
        artifacts / "state_ledger.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    cycles_out = cycles.copy()
    for column in ("entry_date", "exit_date"):
        cycles_out[column] = pd.to_datetime(cycles_out[column]).dt.strftime("%Y-%m-%d")
    cycles_out.to_csv(artifacts / "cycles.csv", index=False, lineterminator="\n")
    (artifacts / "density_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX48 执行\n\n"
        f"状态：`COMPLETE`。形成{metrics['independent_cycles']}个独立状态周期，完整周期"
        f"{metrics['complete_cycles']}个；滚动60日周期数中位数{metrics['rolling_60_median']:.1f}、"
        f"P10为{metrics['rolling_60_p10']:.1f}。本轮未读取状态形成后的588080收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX48 结论\n\n"
        f"裁决：`{decision}`。状态占用率{metrics['state_occupancy']:.2%}，完整周期持有中位数"
        f"{metrics['median_holding_sessions']:.1f}个交易日；频率硬门"
        f"{'通过' if density_pass else '未通过'}。本轮不创建候选、不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": str(protocol["development_cutoff"]),
            "status": "COMPLETE",
            "decision": decision,
            "reads_post_state_prices": False,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
