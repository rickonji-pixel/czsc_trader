"""Execution-aware evaluation for pre-registered relative-style events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from .intraday_opening_execution import _account_metrics, _performance, _trade_return
from .intraday_opportunity_map import _price_checkpoints


@dataclass(frozen=True)
class RelativeStyleEvaluation:
    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    account: pd.DataFrame


def _static_account_metrics(daily_close: pd.Series, base_fraction: float) -> dict[str, float]:
    close = daily_close.astype(float).sort_index()
    equity = base_fraction * close.div(float(close.iloc[0])) + (1.0 - base_fraction)
    drawdown = equity.div(equity.cummax()).sub(1.0)
    years = max((close.index.max() - close.index.min()).days / 365.25, 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0)
    maximum_drawdown = float(drawdown.min())
    return {
        "terminal_equity": float(equity.iloc[-1]),
        "cagr": cagr,
        "max_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else float("inf"),
    }


def evaluate_relative_style_events(
    events: pd.DataFrame,
    five_minute: pd.DataFrame,
    mechanisms: Mapping[str, Mapping[str, str]],
    *,
    evaluation_start: str | pd.Timestamp,
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    base_fraction: float,
    positive_years_required: int,
    recent_sessions: int,
) -> RelativeStyleEvaluation:
    """Evaluate fixed long mappings against opposite and unconditional controls."""

    source = events.copy()
    source["signal_date"] = pd.to_datetime(source["signal_date"]).dt.normalize()
    source["event_date"] = pd.to_datetime(source["event_date"]).dt.normalize()
    if source.duplicated(["mechanism", "event_date"]).any():
        raise ValueError("events must be unique by mechanism and execution day")
    if set(source["mechanism"]) != set(mechanisms):
        raise ValueError("events differ from frozen mechanism definitions")

    checkpoints = _price_checkpoints(five_minute)
    start = pd.Timestamp(evaluation_start).normalize()
    checkpoints = checkpoints.loc[checkpoints.index >= start]
    daily_close = checkpoints["15:00_CLOSE"]
    elapsed_years = max((checkpoints.index.max() - checkpoints.index.min()).days / 365.25, 1.0)
    recent_start = checkpoints.index[-min(recent_sessions, len(checkpoints))]
    static = _static_account_metrics(daily_close, base_fraction)

    episode_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []
    for mechanism, definition in mechanisms.items():
        entry_checkpoint = str(definition["entry_checkpoint"])
        exit_checkpoint = str(definition["exit_checkpoint"])
        if entry_checkpoint not in checkpoints or exit_checkpoint not in checkpoints:
            raise ValueError(f"{mechanism}: unknown execution checkpoint")
        selected = source.loc[source["mechanism"].eq(mechanism)].copy()
        selected = selected.loc[selected["event_date"] >= start].sort_values("event_date")
        selected["entry_price"] = checkpoints[entry_checkpoint].reindex(
            selected["event_date"]
        ).to_numpy()
        selected["exit_price"] = checkpoints[exit_checkpoint].reindex(
            selected["event_date"]
        ).to_numpy()
        if selected[["entry_price", "exit_price"]].isna().any().any():
            raise ValueError(f"{mechanism}: missing execution checkpoint")

        variants: dict[str, pd.DataFrame] = {}
        for variant, direction in (("PRIMARY_LONG", 1), ("OPPOSITE_SHORT", -1)):
            episodes = selected.copy()
            episodes["variant"] = variant
            episodes["direction"] = direction
            for column, cost in (
                ("gross_return", 0.0),
                ("baseline_return", baseline_one_way_cost),
                ("stress_return", stress_one_way_cost),
            ):
                episodes[column] = [
                    _trade_return(float(row.entry_price), float(row.exit_price), direction, cost)
                    for row in episodes.itertuples(index=False)
                ]
            variants[variant] = episodes
            episode_frames.append(episodes)

        unconditional = pd.DataFrame(
            {
                "entry_price": checkpoints[entry_checkpoint],
                "exit_price": checkpoints[exit_checkpoint],
            }
        ).dropna()
        unconditional_baseline = pd.Series(
            [
                _trade_return(float(row.entry_price), float(row.exit_price), 1, baseline_one_way_cost)
                for row in unconditional.itertuples(index=False)
            ],
            dtype=float,
        )
        primary = variants["PRIMARY_LONG"]
        opposite = variants["OPPOSITE_SHORT"]
        gross = _performance(primary["gross_return"], elapsed_years)
        baseline = _performance(primary["baseline_return"], elapsed_years)
        stress = _performance(primary["stress_return"], elapsed_years)
        opposite_stress = _performance(opposite["stress_return"], elapsed_years)
        unconditional_performance = _performance(unconditional_baseline, elapsed_years)
        positive_years = 0
        for year, rows in primary.groupby(primary["event_date"].dt.year, observed=True):
            item = _performance(rows["stress_return"], 1.0)
            annual_rows.append(
                {"mechanism": mechanism, "year": int(year), **item}
            )
            positive_years += int(float(item["mean_return"]) > 0)
        recent = primary.loc[primary["event_date"] >= recent_start]
        recent_stress_mean = (
            float(recent["stress_return"].mean()) if not recent.empty else float("nan")
        )
        account_input = primary.rename(columns={"event_date": "trade_date"})
        account = _account_metrics(account_input, daily_close, base_fraction=base_fraction)
        account_rows.append(
            {
                "mechanism": mechanism,
                **account,
                "static_cagr": static["cagr"],
                "static_max_drawdown": static["max_drawdown"],
                "static_calmar": static["calmar"],
            }
        )
        eligible = bool(
            float(stress["mean_return"]) > 0
            and float(stress["profit_factor"]) > 1
            and float(baseline["mean_return"]) > float(unconditional_performance["mean_return"])
            and float(stress["mean_return"]) > float(opposite_stress["mean_return"])
            and int(positive_years) >= positive_years_required
            and float(account["incremental_terminal_return_vs_static"]) > 0
            and recent_stress_mean > 0
        )
        metric_rows.append(
            {
                "mechanism": mechanism,
                "episodes": int(len(primary)),
                "entry_checkpoint": entry_checkpoint,
                "exit_checkpoint": exit_checkpoint,
                "gross_mean_return": gross["mean_return"],
                "baseline_mean_return": baseline["mean_return"],
                "baseline_profit_factor": baseline["profit_factor"],
                "stress_mean_return": stress["mean_return"],
                "stress_profit_factor": stress["profit_factor"],
                "stress_win_rate": stress["win_rate"],
                "stress_cumulative_return": stress["cumulative_return"],
                "opposite_stress_mean_return": opposite_stress["mean_return"],
                "unconditional_baseline_mean_return": unconditional_performance["mean_return"],
                "positive_years": positive_years,
                "recent_event_count": int(len(recent)),
                "recent_stress_mean_return": recent_stress_mean,
                "incremental_terminal_return_vs_static": account[
                    "incremental_terminal_return_vs_static"
                ],
                "account_max_drawdown": account["max_drawdown"],
                "account_calmar": account["calmar"],
                "static_max_drawdown": static["max_drawdown"],
                "static_calmar": static["calmar"],
                "eligible_for_audit": eligible,
            }
        )

    return RelativeStyleEvaluation(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        account=pd.DataFrame(account_rows),
    )
