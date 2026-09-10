"""Descriptive time-of-day return map for T+1 ETF research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd

from .intraday_opening_execution import _account_metrics, _performance, _trade_return


@dataclass(frozen=True)
class IntradayOpportunityMap:
    """Daily segment returns and their aggregate, annual and account evidence."""

    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    account: pd.DataFrame


def _price_checkpoints(five_minute: pd.DataFrame) -> pd.DataFrame:
    required = {"Date", "Open", "Close"}
    missing = sorted(required.difference(five_minute.columns))
    if missing:
        raise ValueError(f"5m data missing columns {missing}")
    bars = five_minute.loc[:, sorted(required)].copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars = bars.sort_values("Date").reset_index(drop=True)
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    required_clocks = ("09:35", "09:40", "10:30", "10:35", "11:30", "13:05", "15:00")
    indexed = bars.set_index(["trade_date", "clock"])
    days = pd.DatetimeIndex(sorted(bars["trade_date"].unique()), name="trade_date")
    missing_clocks = [
        clock
        for clock in required_clocks
        if len(indexed.loc[indexed.index.get_level_values("clock") == clock]) != len(days)
    ]
    if missing_clocks:
        raise ValueError(f"5m data missing required daily clocks {missing_clocks}")
    if indexed.index.duplicated().any():
        raise ValueError("5m data contains duplicate trading-day clocks")

    checkpoints = pd.DataFrame(index=days)
    checkpoints["PREVIOUS_CLOSE"] = (
        indexed.xs("15:00", level="clock")["Close"].reindex(days).shift(1)
    )
    checkpoints["OPEN"] = indexed.xs("09:35", level="clock")["Open"].reindex(days)
    for clock in ("09:40", "10:35", "13:05"):
        checkpoints[f"{clock}_OPEN"] = indexed.xs(clock, level="clock")["Open"].reindex(days)
    for clock in ("10:30", "11:30", "15:00"):
        checkpoints[f"{clock}_CLOSE"] = indexed.xs(clock, level="clock")["Close"].reindex(days)
    return checkpoints.astype(float)


def build_intraday_opportunity_map(
    five_minute: pd.DataFrame,
    segments: Sequence[Mapping[str, str]],
    *,
    evaluation_start: pd.Timestamp | str,
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    base_fraction: float,
    account_segment_ids: Sequence[str],
) -> IntradayOpportunityMap:
    """Measure fixed time segments without selecting days or fitting parameters."""

    if not 0 < base_fraction < 1:
        raise ValueError("base_fraction must be between zero and one")
    segment_ids = [str(item["segment_id"]) for item in segments]
    if len(segment_ids) != len(set(segment_ids)):
        raise ValueError("segment ids must be unique")
    checkpoints = _price_checkpoints(five_minute)
    start = pd.Timestamp(evaluation_start).normalize()
    checkpoints = checkpoints.loc[checkpoints.index >= start]
    elapsed_years = max(
        (checkpoints.index.max() - checkpoints.index.min()).days / 365.25,
        1.0,
    )
    daily_close = checkpoints["15:00_CLOSE"]

    episode_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []
    account_ids = set(map(str, account_segment_ids))
    if not account_ids.issubset(segment_ids):
        raise ValueError("account segments must be present in the segment map")

    for definition in segments:
        segment_id = str(definition["segment_id"])
        entry_name = str(definition["entry"])
        exit_name = str(definition["exit"])
        if entry_name not in checkpoints or exit_name not in checkpoints:
            raise ValueError(f"{segment_id}: unknown price checkpoint")
        episodes = pd.DataFrame(
            {
                "segment_id": segment_id,
                "trade_date": checkpoints.index,
                "entry_checkpoint": entry_name,
                "exit_checkpoint": exit_name,
                "entry_price": checkpoints[entry_name].to_numpy(),
                "exit_price": checkpoints[exit_name].to_numpy(),
            }
        ).dropna(subset=["entry_price", "exit_price"])
        for column, cost in (
            ("gross_return", 0.0),
            ("baseline_return", baseline_one_way_cost),
            ("stress_return", stress_one_way_cost),
        ):
            episodes[column] = [
                _trade_return(float(row.entry_price), float(row.exit_price), 1, cost)
                for row in episodes.itertuples(index=False)
            ]
        episode_frames.append(episodes)

        gross = _performance(episodes["gross_return"], elapsed_years)
        baseline = _performance(episodes["baseline_return"], elapsed_years)
        stress = _performance(episodes["stress_return"], elapsed_years)
        break_even_cost = max(float(gross["mean_return"]), 0.0) / (
            2.0 + max(float(gross["mean_return"]), 0.0)
        )
        positive_years = {"gross": 0, "baseline": 0, "stress": 0}
        for year, rows in episodes.groupby(episodes["trade_date"].dt.year, observed=True):
            annual_item: dict[str, object] = {
                "segment_id": segment_id,
                "year": int(year),
                "episodes": int(len(rows)),
            }
            for label in positive_years:
                performance = _performance(rows[f"{label}_return"], 1.0)
                annual_item[f"{label}_mean_return"] = performance["mean_return"]
                annual_item[f"{label}_win_rate"] = performance["win_rate"]
                annual_item[f"{label}_cumulative_return"] = performance["cumulative_return"]
                positive_years[label] += int(float(performance["mean_return"]) > 0)
            annual_rows.append(annual_item)
        metric_rows.append(
            {
                "segment_id": segment_id,
                "episodes": int(len(episodes)),
                "gross_mean_return": gross["mean_return"],
                "gross_median_return": gross["median_return"],
                "gross_win_rate": gross["win_rate"],
                "gross_cumulative_return": gross["cumulative_return"],
                "baseline_mean_return": baseline["mean_return"],
                "baseline_profit_factor": baseline["profit_factor"],
                "stress_mean_return": stress["mean_return"],
                "stress_profit_factor": stress["profit_factor"],
                "break_even_one_way_cost_bp": break_even_cost * 10_000.0,
                "gross_positive_years": positive_years["gross"],
                "baseline_positive_years": positive_years["baseline"],
                "stress_positive_years": positive_years["stress"],
            }
        )
        if segment_id in account_ids:
            for cost_label in ("baseline", "stress"):
                account_input = episodes.loc[:, ["trade_date", f"{cost_label}_return"]].rename(
                    columns={f"{cost_label}_return": "stress_return"}
                )
                account_rows.append(
                    {
                        "segment_id": segment_id,
                        "cost_label": cost_label.upper(),
                        **_account_metrics(
                            account_input,
                            daily_close,
                            base_fraction=base_fraction,
                        ),
                    }
                )

    return IntradayOpportunityMap(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        account=pd.DataFrame(account_rows),
    )
