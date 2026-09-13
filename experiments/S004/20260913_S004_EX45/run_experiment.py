from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX45"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0.0].sum())
    losses = float(-values.loc[values < 0.0].sum())
    return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("parameter_selection", "candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("local platform audit cannot select, promote or deploy")
    for source_id in ("20260912_S004_EX12", "20260913_S004_EX37", "20260913_S004_EX40", "20260913_S004_EX41"):
        validate_experiment_archive(repo / "experiments" / "S004" / source_id)

    base = pd.read_csv(
        repo / "experiments/S004/20260912_S004_EX12/artifacts/candidate_episodes.csv.gz",
        parse_dates=["event_date", "entry_date"],
    )
    overlay = pd.read_csv(
        repo / "experiments/S004/20260913_S004_EX40/artifacts/overlay_episodes.csv.gz",
        parse_dates=["event_date"],
    )[["event_date", "low_volatility"]].drop_duplicates()
    base = base.merge(overlay, on="event_date", validate="one_to_one")
    panel = pd.read_csv(
        repo / "experiments/S004/20260913_S004_EX37/artifacts/event_panel.csv.gz",
        parse_dates=["source_date"],
    )
    market = load_market_data(repo / "data/raw", str(protocol["symbol"])).daily.copy()
    market["event_date"] = pd.to_datetime(market["dt"]).dt.normalize()
    calendar = pd.DatetimeIndex(
        market.loc[
            market["event_date"].le(pd.Timestamp(str(protocol["development_cutoff"]))),
            "event_date",
        ].sort_values()
    )
    base_mean = float(base["stress_return"].mean())
    base_pf = _profit_factor(base["stress_return"])
    recent_start = pd.Timestamp(calendar[-252])
    rows = []
    for quantile in protocol["diagnostic_quantiles"]:
        label = f"Q{int(round(float(quantile) * 100))}"
        risk_dates = set(panel.loc[panel[f"ACCUM_{label}"].astype(bool), "source_date"])
        filtered = base.loc[~base["event_date"].isin(risk_dates)].copy()
        annual = filtered.groupby(filtered["entry_date"].dt.year)["stress_return"].mean()
        low = filtered.loc[filtered["low_volatility"].astype(bool), "stress_return"]
        recent = filtered.loc[filtered["event_date"].ge(recent_start), "stress_return"]
        flags = pd.Series(0, index=calendar[calendar >= pd.Timestamp("2021-06-01")], dtype=int)
        flags.loc[flags.index.intersection(pd.DatetimeIndex(filtered["event_date"]))] = 1
        rolling = flags.rolling(60, min_periods=60).sum().dropna()
        mean = float(filtered["stress_return"].mean())
        profit_factor = _profit_factor(filtered["stress_return"])
        checks = {
            "positive_mean": mean > float(protocol["acceptance"]["stress_mean_min_exclusive"]),
            "profit_factor": profit_factor > float(protocol["acceptance"]["profit_factor_min_exclusive"]),
            "positive_years": int(annual.gt(0.0).sum()) >= int(protocol["acceptance"]["positive_years_minimum"]),
            "recent_positive": float(recent.mean()) > float(protocol["acceptance"]["recent_252_mean_min_exclusive"]),
            "improves_base": mean > base_mean and profit_factor > base_pf,
        }
        rows.append(
            {
                "quantile": float(quantile),
                "cell": label,
                "is_center": float(quantile) == float(protocol["center_quantile"]),
                "closed_trades": int(len(filtered)),
                "removed_trades": int(len(base) - len(filtered)),
                "stress_mean": mean,
                "profit_factor": profit_factor,
                "positive_years": int(annual.gt(0.0).sum()),
                "recent_252_mean": float(recent.mean()),
                "rolling_60_median": float(rolling.median()),
                "rolling_60_p10": float(rolling.quantile(0.10, interpolation="lower")),
                "low_vol_trades": int(len(low)),
                "low_vol_mean": float(low.mean()),
                "low_vol_profit_factor": _profit_factor(low),
                **{f"check_{key}": bool(value) for key, value in checks.items()},
                "passes": all(checks.values()),
            }
        )
    grid = pd.DataFrame(rows).sort_values("quantile")
    passing = int(grid["passes"].sum())
    center = grid.loc[grid["is_center"]].iloc[0]
    adjacent = grid.loc[grid["quantile"].isin([0.55, 0.65])]
    favorable = (
        passing >= int(protocol["acceptance"]["favorable_minimum_passing_cells"])
        and bool(center["passes"])
        and bool(adjacent["passes"].all())
    )
    mixed = bool(center["passes"]) and passing >= int(protocol["acceptance"]["mixed_minimum_passing_cells"])
    label = "FAVORABLE" if favorable else "MIXED" if mixed else "WEAK"
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": label,
        "passing_cells": passing,
        "tested_cells": int(len(grid)),
        "center_passes": bool(center["passes"]),
        "adjacent_cells_pass": bool(adjacent["passes"].all()),
        "base_stress_mean": base_mean,
        "base_profit_factor": base_pf,
        "frequency_policy": "OBSERVATION_ONLY",
        "candidate_unchanged": True,
    }
    grid.to_csv(artifacts / "platform_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "platform_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# S004 EX45 执行\n\n状态：COMPLETE。Q55/Q60/Q65/Q70 共 {len(grid)} 格完成固定邻域审计，{passing} 格通过质量检查。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S004 EX45 结论\n\n参数稳健性：`{label}`。中心 Q60 {'通过' if bool(center['passes']) else '未通过'}，"
        f"相邻 Q55/Q65 {'均通过' if bool(adjacent['passes'].all()) else '未全部通过'}；频率只作观察。"
        "本轮未重选阈值，候选定义保持不变。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": protocol["symbol"],
            "candidate_id": protocol["candidate_id"],
            "development_cutoff": protocol["development_cutoff"],
            "evidence_label": label,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
