from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_opening_execution import evaluate_opening_shock_directions


EXPERIMENT_ID = "20260911_S003_EX13"


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


def _select_events(events: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    source = events.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"]).dt.normalize()
    regime = regimes.loc[:, ["date", "regime"]].copy()
    regime["trade_date"] = pd.to_datetime(regime.pop("date")).dt.normalize()
    selected = source.merge(regime, on="trade_date", how="left", validate="one_to_one")
    if selected["regime"].isna().any():
        raise ValueError("event date missing frozen daily regime")
    long_overlay = (
        (selected["regime"].eq("range") & selected["pressure_side"].eq("sell_pressure"))
        | (selected["regime"].eq("trend_up") & selected["pressure_side"].eq("sell_pressure"))
    )
    short_overlay = (
        (selected["regime"].eq("range") & selected["pressure_side"].eq("buy_pressure"))
        | (selected["regime"].eq("trend_down") & selected["pressure_side"].eq("buy_pressure"))
    )
    selected = selected.loc[long_overlay | short_overlay].copy()
    selected["shock_side"] = "down"
    selected.loc[long_overlay.loc[selected.index], "shock_side"] = "up"
    if selected["trade_date"].duplicated().any():
        raise ValueError("regime pullback selection generated duplicate dates")
    return selected


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX13 may not mutate strategy lifecycle state")
    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    density = protocol["density"]
    acceptance = protocol["acceptance"]
    event_dir = repo / "experiments" / str(dataset["event_experiment"])
    regime_dir = repo / "experiments" / str(dataset["regime_experiment"])
    event_manifest = validate_experiment_archive(event_dir)
    regime_manifest = validate_experiment_archive(regime_dir)
    events = pd.read_csv(event_dir / "artifacts" / "volume_imbalance_events.csv")
    regimes = pd.read_csv(regime_dir / "artifacts" / "daily_regimes.csv")
    selected = _select_events(events, regimes)
    selected.to_csv(
        artifacts / "selected_events.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    daily_regime = regimes.set_index(pd.to_datetime(regimes["date"]))["regime"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    result = evaluate_opening_shock_directions(
        selected,
        intraday.frames["5m"],
        daily_regime,
        protocol["mechanisms"],
        evaluation_start=str(dataset["evaluation_start"]),
        exit_clocks=execution["exit_clocks"],
        baseline_one_way_cost=float(execution["baseline_one_way_cost"]),
        stress_one_way_cost=float(execution["stress_one_way_cost"]),
        base_fraction=float(execution["base_fraction"]),
        density_window_sessions=int(density["window_sessions"]),
        median_episodes_required=int(density["median_closed_episodes_required"]),
        p10_episodes_required=int(density["p10_closed_episodes_required"]),
        positive_years_required=int(acceptance["positive_years_required"]),
        entry_clock=str(execution["entry_clock"]),
    )
    for name, frame in {
        "episodes.csv.gz": result.episodes,
        "mechanism_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "regime_metrics.csv": result.regime,
        "account_metrics.csv": result.account,
    }.items():
        compression = {"method": "gzip", "compresslevel": 9, "mtime": 0} if name.endswith(".gz") else None
        frame.to_csv(
            artifacts / name,
            index=False,
            encoding="utf-8-sig",
            compression=compression,
            lineterminator="\n",
        )
    feasible = result.metrics.loc[result.metrics["evidence"].eq("FEASIBLE")]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_manifest_sha256": event_manifest["files"]["artifacts/volume_imbalance_events.csv"]["sha256"],
        "regime_manifest_sha256": regime_manifest["files"]["artifacts/daily_regimes.csv"]["sha256"],
        "source_events": int(len(events)),
        "selected_events": int(len(selected)),
        "evaluated_combinations": int(len(result.metrics)),
        "feasible_combinations": int(len(feasible)),
        "route_decision": "CONTINUE_CROSS_SECTION" if len(feasible) else "STOP_REGIME_PULLBACK_ROUTE",
        "candidate_created": False,
    }
    _write(artifacts / "regime_pullback_summary.json", summary)
    table = [
        "|机制|笔数|60日中位/P10|基准均值|压力均值|压力盈亏比|正收益年份|相对静态终值|标签|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in result.metrics.sort_values("stress_mean_return", ascending=False).itertuples(index=False):
        table.append(
            f"|{row.mechanism_id}|{row.episodes}|{row.rolling_median_episodes:.0f}/"
            f"{row.rolling_p10_episodes:.0f}|{row.baseline_mean_return:.3%}|"
            f"{row.stress_mean_return:.3%}|{row.stress_profit_factor:.2f}|"
            f"{row.positive_years}|{row.incremental_terminal_return_vs_static:.1%}|{row.evidence}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S003 EX13 执行\n\n"
        f"状态：COMPLETE。EX11的{len(events)}个事件经冻结regime规则自然筛成{len(selected)}笔，"
        f"与相反方向对照使用完全相同的日期和价格；{len(feasible)}个机制达到FEASIBLE。\n",
        encoding="utf-8",
    )
    decision = (
        "regime回撤机制通过中频可行性门槛，下一步应先做横截面复现，再决定是否进入候选评价。"
        if len(feasible)
        else "regime回撤与相反方向均未通过，中频regime条件化路线停止。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX13 结论\n\n" + "\n".join(table) + "\n\n" + decision
        + " 本实验没有修改S003全局契约，没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
