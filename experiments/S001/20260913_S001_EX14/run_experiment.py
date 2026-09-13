from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.factors import generate_factor_frame, map_signal_frame
from czsc_trader.strategy_runtime import apply_resolved_strategy


EXPERIMENT_ID = "20260913_S001_EX14"


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


def _event_path(
    states: pd.Series,
    baseline_target: pd.Series,
    opens: pd.Series,
    direction: float,
    holding_sessions: int,
    fee_rate: float,
) -> pd.DataFrame:
    index = opens.index
    transitions = states.eq(direction) & states.shift(1).ne(direction)
    rows: list[dict[str, object]] = []
    last_exit = -1
    for signal_date in index[transitions.reindex(index, fill_value=False)]:
        signal_index = int(index.get_loc(signal_date))
        entry_index = signal_index + 1
        exit_index = entry_index + holding_sessions
        if entry_index <= last_exit or exit_index >= len(index):
            continue
        if float(baseline_target.loc[signal_date]) != 0.0:
            continue
        entry_date = index[entry_index]
        exit_date = index[exit_index]
        entry_price = float(opens.iloc[entry_index])
        exit_price = float(opens.iloc[exit_index])
        net_return = exit_price * (1.0 - fee_rate) / (entry_price * (1.0 + fee_rate)) - 1.0
        baseline_conflict = bool(
            baseline_target.iloc[entry_index : exit_index + 1].astype(float).gt(0).any()
        )
        rows.append(
            {
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "net_return": net_return,
                "baseline_entry_before_exit": baseline_conflict,
            }
        )
        last_exit = exit_index
    return pd.DataFrame(rows)


def _metrics(events: pd.DataFrame, sessions: int) -> dict[str, object]:
    if events.empty:
        return {
            "closed_events": 0,
            "closed_events_per_60_sessions": 0.0,
            "mean_net_return": None,
            "median_net_return": None,
            "win_rate": None,
            "profit_factor": None,
            "worst_event": None,
            "positive_years": 0,
            "observed_years": 0,
            "baseline_conflict_rate": None,
        }
    returns = events["net_return"].astype(float)
    wins = returns.loc[returns.gt(0)]
    losses = returns.loc[returns.lt(0)]
    yearly = events.assign(year=events["signal_date"].dt.year).groupby("year")[
        "net_return"
    ].mean()
    return {
        "closed_events": int(len(events)),
        "closed_events_per_60_sessions": float(len(events) * 60 / sessions),
        "mean_net_return": float(returns.mean()),
        "median_net_return": float(returns.median()),
        "win_rate": float(returns.gt(0).mean()),
        "profit_factor": (
            float(wins.sum() / abs(losses.sum())) if not wins.empty and not losses.empty else None
        ),
        "worst_event": float(returns.min()),
        "positive_years": int(yearly.gt(0).sum()),
        "observed_years": int(len(yearly)),
        "baseline_conflict_rate": float(events["baseline_entry_before_exit"].mean()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    context = RepositoryContext.discover(repo)
    source = resolve_registered_strategy(
        context, str(protocol["strategy_id"]), str(protocol["source_version"])
    )
    if source.source_hash != protocol["source_release_hash"]:
        raise ValueError("source strategy release differs from frozen protocol")
    data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        pd.Timestamp(str(protocol["development_cutoff"])).date(),
    )
    start = pd.Timestamp(str(protocol["evaluation_start"]))
    end = pd.Timestamp(str(protocol["evaluation_end"]))
    factor_frame = generate_factor_frame(data.adjusted).frame
    factor_names = list(source.resolved_rule.factor_names)
    mapped, unknown = map_signal_frame(factor_frame[factor_names])
    applied = apply_resolved_strategy(data.adjusted, source.resolved_rule)
    daily = data.execution_daily.set_index("dt").sort_index().loc[start:end]
    sessions = pd.DatetimeIndex(daily.index)
    opens = daily["open"].astype(float)
    baseline_target = applied.target_position.reindex(sessions).astype(float)
    if baseline_target.isna().any():
        raise ValueError("baseline target does not cover the evaluation window")

    event_pieces: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    screen = dict(protocol["screen"])
    trial = 0
    for factor_name in factor_names:
        states = mapped[factor_name].reindex(sessions).fillna(0.0)
        for direction_name, direction_value in dict(protocol["event_directions"]).items():
            for holding in map(int, protocol["holding_sessions"]):
                trial += 1
                path_id = f"P{trial:03d}"
                events = _event_path(
                    states,
                    baseline_target,
                    opens,
                    float(direction_value),
                    holding,
                    float(protocol["fee_rate"]),
                )
                metrics = _metrics(events, len(sessions))
                screen_pass = bool(
                    metrics["closed_events_per_60_sessions"]
                    >= float(screen["minimum_closed_events_per_60_sessions"])
                    and metrics["mean_net_return"] is not None
                    and metrics["mean_net_return"] > float(screen["minimum_mean_net_return"])
                    and metrics["positive_years"] >= int(screen["minimum_positive_years"])
                )
                metric_rows.append(
                    {
                        "path_id": path_id,
                        "factor_name": factor_name,
                        "direction": direction_name,
                        "holding_sessions": holding,
                        **metrics,
                        "screen_pass": screen_pass,
                    }
                )
                if not events.empty:
                    event_pieces.append(
                        events.assign(
                            path_id=path_id,
                            factor_name=factor_name,
                            direction=direction_name,
                            holding_sessions=holding,
                        )
                    )
    metrics = pd.DataFrame(metric_rows)
    if len(metrics) != 72:
        raise AssertionError("event census trial count differs from protocol")
    events = pd.concat(event_pieces, ignore_index=True)
    metrics.to_csv(artifacts / "event_path_metrics.csv", index=False, lineterminator="\n")
    events.to_csv(artifacts / "event_ledger.csv", index=False, lineterminator="\n")
    passed = metrics.loc[metrics["screen_pass"]].copy()
    density_max = metrics.sort_values("closed_events_per_60_sessions", ascending=False).iloc[0]
    positive = metrics.loc[metrics["mean_net_return"].fillna(-np.inf).gt(0)]
    best_positive = (
        None
        if positive.empty
        else positive.sort_values(
            ["closed_events_per_60_sessions", "mean_net_return"], ascending=False
        ).iloc[0].to_dict()
    )
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "source_strategy": source.identity.reference,
        "factor_count": len(factor_names),
        "trial_count": len(metrics),
        "unknown_signal_values": unknown,
        "screen_pass_count": len(passed),
        "screen_pass_paths": passed["path_id"].astype(str).tolist(),
        "highest_density_path": density_max.to_dict(),
        "highest_density_positive_path": best_positive,
        "route_decision": (
            "PROCEED_TO_COMPLEMENTARY_EVENT_VALIDATION"
            if not passed.empty
            else "CURRENT_FACTOR_UNIVERSE_CANNOT_MEET_FREQUENCY_TARGET"
        ),
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    _write(artifacts / "census_summary.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX14 执行\n\n状态：COMPLETE。已完成72条预登记事件路径，"
        "保留完整事件账本；未组合规则或生成候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX14 结论\n\n"
        f"满足频率、费后收益和年度方向三项筛选的路径共{len(passed)}条。"
        f"裁决：`{result['route_decision']}`。"
        "高密度事件与S001基线入场冲突率已单独披露，不把重叠事件当成独立证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["source_version"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": result["route_decision"],
            "candidate_generation": False,
            "trial_count_added": len(metrics),
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
