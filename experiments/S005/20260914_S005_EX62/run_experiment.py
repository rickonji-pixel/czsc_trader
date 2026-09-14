from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX62"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _historical_percentile(series: pd.Series, history: int) -> pd.Series:
    return series.astype(float).rolling(history + 1, min_periods=history + 1).apply(
        lambda window: float(np.mean(window[:-1] <= window[-1])), raw=True
    )


def _cooldown_events(raw: pd.Series, cooldown: int) -> pd.Series:
    selected = pd.Series(False, index=raw.index)
    last_index = -cooldown - 1
    for index, active in enumerate(raw.fillna(False).astype(bool)):
        if active and index - last_index > cooldown:
            selected.iloc[index] = True
            last_index = index
    return selected


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_post_event_returns"):
        raise ValueError("EX62 must match the frozen return-free protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX62 cannot create, promote, or deploy a candidate")
    for path, expected in (
        (repo / "catalog/factors/project.json", protocol["catalog"]["project_factors_sha256"]),
        (repo / "catalog/signals/project.json", protocol["catalog"]["project_signals_sha256"]),
    ):
        if _sha256(path) != expected:
            raise ValueError(f"catalog differs from frozen protocol: {path}")
    source = repo / "experiments/S005" / str(protocol["source"]["experiment_id"])
    validate_experiment_archive(source)
    for path, expected in (
        (source / "experiment_manifest.json", protocol["source"]["manifest_sha256"]),
        (source / "artifacts/factor_observation_ledger.csv.gz", protocol["source"]["factor_observation_ledger_sha256"]),
        (source / "artifacts/materialization_evidence.json", protocol["source"]["materialization_evidence_sha256"]),
    ):
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")

    ledger = pd.read_csv(source / "artifacts/factor_observation_ledger.csv.gz")
    factor_ids = [factor_id for group in protocol["factor_groups"].values() for factor_id in group]
    selected = ledger.loc[ledger["factor_id"].isin(factor_ids) & ledger["value_numeric"].notna()].copy()
    selected["first_usable_date"] = pd.to_datetime(selected["first_usable_date"]).dt.normalize()
    if selected.duplicated(["factor_id", "first_usable_date"]).any():
        raise ValueError("factor ledger contains duplicate factor-session values")
    panel = selected.pivot(index="first_usable_date", columns="factor_id", values="value_numeric").sort_index()
    panel = panel.loc[panel.index <= pd.Timestamp(protocol["development_cutoff"])].dropna(subset=factor_ids)
    if panel.empty:
        raise ValueError("project factors have no common complete window")

    history = int(protocol["factor_history_sessions"])
    percentile = pd.DataFrame(index=panel.index)
    for factor_id in factor_ids:
        percentile[factor_id] = _historical_percentile(panel[factor_id], history)
    panel["prior_supply_pressure_score"] = 1.0 - percentile[protocol["factor_groups"]["prior_supply_pressure"]].mean(axis=1)
    panel["first_hour_absorption_score"] = percentile[protocol["factor_groups"]["first_hour_absorption"]].mean(axis=1)
    left = panel["prior_supply_pressure_score"]
    right = panel["first_hour_absorption_score"]
    panel["supply_absorption_score"] = np.where(
        left.gt(0) & right.gt(0), 2.0 * left * right / (left + right), 0.0
    )
    score_columns = ["prior_supply_pressure_score", "first_hour_absorption_score"]

    density_rows: list[dict[str, object]] = []
    event_frames: list[pd.DataFrame] = []
    eligible_start: pd.Timestamp | None = None
    for quantile in protocol["score_quantiles"]:
        label = f"SUPPLY_ABSORPTION_Q{int(round(float(quantile) * 100))}"
        threshold = panel["supply_absorption_score"].shift(1).rolling(history, min_periods=history).quantile(float(quantile))
        valid = threshold.notna() & panel[score_columns].notna().all(axis=1)
        raw = panel["supply_absorption_score"].ge(threshold) & valid
        events = _cooldown_events(raw, int(protocol["event_cooldown_sessions"]))
        if not valid.any():
            raise ValueError(f"{label}: no calibrated observations")
        current_start = panel.index[valid][0]
        eligible_start = current_start if eligible_start is None else min(eligible_start, current_start)
        rolling = events.loc[valid].astype(int).rolling(
            int(protocol["density"]["window_sessions"]), min_periods=int(protocol["density"]["window_sessions"])
        ).sum().dropna()
        median = float(rolling.median())
        p10 = float(rolling.quantile(0.10, interpolation="lower"))
        passed = median >= float(protocol["density"]["median_minimum"]) and p10 >= float(protocol["density"]["p10_minimum"])
        density_rows.append({
            "mechanism": label,
            "score_quantile": float(quantile),
            "calibrated_start": current_start.date().isoformat(),
            "independent_events": int(events.loc[valid].sum()),
            "rolling_60_median": median,
            "rolling_60_p10": p10,
            "rolling_60_min": float(rolling.min()),
            "rolling_60_max": float(rolling.max()),
            "density_pass": bool(passed),
        })
        event_rows = panel.loc[events & valid, score_columns + ["supply_absorption_score"]].reset_index(names="event_date")
        event_rows.insert(0, "mechanism", label)
        event_rows["threshold"] = threshold.loc[events & valid].to_numpy()
        event_frames.append(event_rows)
        panel[f"{label}_threshold"] = threshold
        panel[f"{label}_event"] = events

    density = pd.DataFrame(density_rows).sort_values("score_quantile")
    passing = density.loc[density["density_pass"]].sort_values("score_quantile", ascending=False)
    selected_mechanism = None if passing.empty else str(passing.iloc[0]["mechanism"])
    decision = "STOP_SUPPLY_ABSORPTION_ON_DENSITY" if selected_mechanism is None else "PROCEED_TO_FIXED_SUPPLY_ABSORPTION_RETURN_TEST"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "common_factor_start": panel.index.min().date().isoformat(),
        "common_factor_end": panel.index.max().date().isoformat(),
        "fully_calibrated_start": eligible_start.date().isoformat() if eligible_start is not None else None,
        "common_sessions": int(len(panel)),
        "information_families": [
            "MARKET_BREADTH", "FUND_FLOW", "TREND_MOMENTUM", "POSITION_VALUATION",
            "MARKET_MICROSTRUCTURE", "VOLUME_LIQUIDITY",
        ],
        "factor_count": len(factor_ids),
        "group_count": len(protocol["factor_groups"]),
        "selected_mechanism": selected_mechanism,
        "reads_post_event_returns": False,
        "prior_cumulative_return_path_count": protocol["prior_cumulative_return_path_count"],
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    panel.reset_index(names="first_usable_date").to_csv(
        artifacts / "supply_absorption_panel.csv.gz", index=False, compression=compression, lineterminator="\n"
    )
    pd.concat(event_frames, ignore_index=True).to_csv(
        artifacts / "event_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    density.to_csv(artifacts / "density_evidence.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "selection_evidence.json", evidence)

    table = ["|机制|事件|60日中位/P10|最小/最大|密度门|", "|---|---:|---:|---:|---|"]
    for row in density.itertuples(index=False):
        table.append(
            f"|{row.mechanism}|{row.independent_events}|{row.rolling_60_median:.0f}/{row.rolling_60_p10:.0f}|"
            f"{row.rolling_60_min:.0f}/{row.rolling_60_max:.0f}|{'PASS' if row.density_pass else 'FAIL'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX62 执行\n\n状态：`COMPLETE`。卖压吸收机制完成无收益密度筛选。\n\n" + "\n".join(table) + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX62 结论\n\n" + "\n".join(table) + "\n\n"
        f"裁决：`{decision}`。唯一收益检验资格：{selected_mechanism or 'NONE'}。"
        "本轮只确认事件供给，没有读取收益、生成候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S005",
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
