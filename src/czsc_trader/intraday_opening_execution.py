"""Execution research for pre-registered ETF opening-shock events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd


@dataclass(frozen=True)
class OpeningShockEvaluationResult:
    """Episode, aggregate, annual, regime and account evidence."""

    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    regime: pd.DataFrame
    account: pd.DataFrame


def _performance(returns: pd.Series, elapsed_years: float) -> dict[str, float]:
    values = pd.to_numeric(returns, errors="raise").astype(float)
    if values.empty:
        return {
            "episodes": 0,
            "mean_return": float("nan"),
            "median_return": float("nan"),
            "win_rate": float("nan"),
            "profit_factor": float("nan"),
            "cumulative_return": float("nan"),
            "annualized_return": float("nan"),
        }
    positive = float(values.loc[values > 0].sum())
    negative = float(-values.loc[values < 0].sum())
    profit_factor = positive / negative if negative > 0 else float("inf")
    cumulative = float((1.0 + values).prod() - 1.0)
    annualized = float((1.0 + cumulative) ** (1.0 / max(elapsed_years, 1.0)) - 1.0)
    return {
        "episodes": int(len(values)),
        "mean_return": float(values.mean()),
        "median_return": float(values.median()),
        "win_rate": float(values.gt(0).mean()),
        "profit_factor": profit_factor,
        "cumulative_return": cumulative,
        "annualized_return": annualized,
    }


def _density(
    event_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
    window_sessions: int,
) -> tuple[float, float, float, float]:
    counts = pd.Series(0, index=calendar, dtype="int64")
    counts.loc[counts.index.intersection(event_dates)] = 1
    rolling = counts.rolling(window_sessions, min_periods=window_sessions).sum().dropna()
    if rolling.empty:
        return 0.0, 0.0, 0.0, 0.0
    return (
        float(rolling.median()),
        float(rolling.quantile(0.10, interpolation="lower")),
        float(rolling.min()),
        float(rolling.max()),
    )


def _trade_return(
    entry_price: float,
    exit_price: float,
    direction: int,
    one_way_cost: float,
) -> float:
    if direction == 1:
        return exit_price * (1.0 - one_way_cost) / (
            entry_price * (1.0 + one_way_cost)
        ) - 1.0
    if direction == -1:
        return entry_price * (1.0 - one_way_cost) / (
            exit_price * (1.0 + one_way_cost)
        ) - 1.0
    raise ValueError("direction must be 1 or -1")


def _account_metrics(
    episodes: pd.DataFrame,
    daily_close: pd.Series,
    *,
    base_fraction: float,
) -> dict[str, float]:
    close = daily_close.astype(float).sort_index()
    base = base_fraction * close.div(float(close.iloc[0]))
    overlay_returns = pd.Series(0.0, index=close.index)
    realized = episodes.set_index("trade_date")["stress_return"]
    overlap = overlay_returns.index.intersection(realized.index)
    overlay_returns.loc[overlap] = realized.reindex(overlap)
    overlay = (1.0 - base_fraction) * (1.0 + overlay_returns).cumprod()
    equity = base + overlay
    static = base + (1.0 - base_fraction)
    drawdown = equity.div(equity.cummax()).sub(1.0)
    years = max((close.index.max() - close.index.min()).days / 365.25, 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0)
    maximum_drawdown = float(drawdown.min())
    return {
        "terminal_equity": float(equity.iloc[-1]),
        "cagr": cagr,
        "max_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else float("inf"),
        "static_terminal_equity": float(static.iloc[-1]),
        "incremental_terminal_return_vs_static": float(equity.iloc[-1] / static.iloc[-1] - 1.0),
    }


def evaluate_opening_shock_directions(
    events: pd.DataFrame,
    five_minute: pd.DataFrame,
    daily_regime: pd.Series,
    mechanisms: Sequence[Mapping[str, object]],
    *,
    evaluation_start: pd.Timestamp | str,
    exit_clocks: Sequence[str],
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    base_fraction: float,
    density_window_sessions: int,
    median_episodes_required: int,
    p10_episodes_required: int,
    positive_years_required: int,
    entry_clock: str = "09:50",
) -> OpeningShockEvaluationResult:
    """Compare fixed reversal and continuation mappings on identical events."""

    if not 0 < base_fraction < 1:
        raise ValueError("base_fraction must be between zero and one")
    source = events.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"]).dt.normalize()
    source["event_time"] = pd.to_datetime(source["event_time"])
    if source["trade_date"].duplicated().any():
        raise ValueError("opening events must be unique by trading day")
    bars = five_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"])
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    start = pd.Timestamp(evaluation_start).normalize()
    calendar = pd.DatetimeIndex(
        sorted(bars.loc[bars["trade_date"] >= start, "trade_date"].unique()),
        name="trade_date",
    )
    regimes = daily_regime.copy()
    regimes.index = pd.DatetimeIndex(pd.to_datetime(regimes.index)).normalize()
    entry = bars.loc[bars["clock"].eq(entry_clock)].set_index("trade_date")["Open"]
    daily_close = bars.loc[bars["clock"].eq("15:00")].set_index("trade_date")["Close"]
    elapsed_years = max((calendar.max() - calendar.min()).days / 365.25, 1.0)
    median_count, p10_count, minimum_count, maximum_count = _density(
        pd.DatetimeIndex(source["trade_date"]), calendar, density_window_sessions
    )
    density_pass = bool(
        median_count >= median_episodes_required and p10_count >= p10_episodes_required
    )

    episode_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    regime_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []
    provisional: dict[tuple[str, str], dict[str, object]] = {}
    mechanism_ids = [str(item["mechanism_id"]) for item in mechanisms]
    if len(mechanism_ids) != 2 or len(set(mechanism_ids)) != 2:
        raise ValueError("direction competition requires exactly two mechanisms")

    for mechanism in mechanisms:
        mechanism_id = str(mechanism["mechanism_id"])
        mapping = {
            "down": int(mechanism["down_shock_direction"]),
            "up": int(mechanism["up_shock_direction"]),
        }
        if sorted(mapping.values()) != [-1, 1]:
            raise ValueError("each mechanism must map shocks to opposite directions")
        for exit_clock in exit_clocks:
            exits = bars.loc[bars["clock"].eq(str(exit_clock))].set_index("trade_date")["Close"]
            episodes = source.loc[source["trade_date"] >= start].copy()
            episodes["entry_price"] = entry.reindex(episodes["trade_date"]).to_numpy()
            episodes["exit_price"] = exits.reindex(episodes["trade_date"]).to_numpy()
            if episodes[["entry_price", "exit_price"]].isna().any().any():
                raise ValueError(f"missing entry or {exit_clock} exit price")
            episodes["direction"] = episodes["shock_side"].map(mapping).astype(int)
            episodes["gross_return"] = [
                _trade_return(float(row.entry_price), float(row.exit_price), int(row.direction), 0.0)
                for row in episodes.itertuples(index=False)
            ]
            episodes["baseline_return"] = [
                _trade_return(
                    float(row.entry_price),
                    float(row.exit_price),
                    int(row.direction),
                    baseline_one_way_cost,
                )
                for row in episodes.itertuples(index=False)
            ]
            episodes["stress_return"] = [
                _trade_return(
                    float(row.entry_price),
                    float(row.exit_price),
                    int(row.direction),
                    stress_one_way_cost,
                )
                for row in episodes.itertuples(index=False)
            ]
            episodes["regime"] = regimes.reindex(episodes["trade_date"]).to_numpy()
            episodes.insert(0, "mechanism_id", mechanism_id)
            episodes.insert(1, "exit_clock", str(exit_clock))
            episode_frames.append(episodes)

            stress = _performance(episodes["stress_return"], elapsed_years)
            baseline = _performance(episodes["baseline_return"], elapsed_years)
            gross = _performance(episodes["gross_return"], elapsed_years)
            positive_years = 0
            for year, rows in episodes.groupby(episodes["trade_date"].dt.year, observed=True):
                item = _performance(rows["stress_return"], 1.0)
                annual_rows.append(
                    {
                        "mechanism_id": mechanism_id,
                        "exit_clock": str(exit_clock),
                        "year": int(year),
                        **item,
                    }
                )
                positive_years += int(float(item["mean_return"]) > 0)
            for regime, rows in episodes.groupby("regime", observed=True):
                regime_rows.append(
                    {
                        "mechanism_id": mechanism_id,
                        "exit_clock": str(exit_clock),
                        "regime": str(regime),
                        **_performance(rows["stress_return"], elapsed_years),
                    }
                )
            account = _account_metrics(episodes, daily_close.reindex(calendar), base_fraction=base_fraction)
            account_rows.append(
                {"mechanism_id": mechanism_id, "exit_clock": str(exit_clock), **account}
            )
            provisional[(mechanism_id, str(exit_clock))] = {
                "mechanism_id": mechanism_id,
                "exit_clock": str(exit_clock),
                "episodes": int(stress["episodes"]),
                "gross_mean_return": float(gross["mean_return"]),
                "baseline_mean_return": float(baseline["mean_return"]),
                "baseline_profit_factor": float(baseline["profit_factor"]),
                "stress_mean_return": float(stress["mean_return"]),
                "stress_profit_factor": float(stress["profit_factor"]),
                "stress_win_rate": float(stress["win_rate"]),
                "stress_cumulative_return": float(stress["cumulative_return"]),
                "positive_years": positive_years,
                "rolling_window_sessions": int(density_window_sessions),
                "rolling_median_episodes": median_count,
                "rolling_p10_episodes": p10_count,
                "rolling_min_episodes": minimum_count,
                "rolling_max_episodes": maximum_count,
                "density_pass": density_pass,
                "incremental_terminal_return_vs_static": account[
                    "incremental_terminal_return_vs_static"
                ],
            }

    for (mechanism_id, exit_clock), row in provisional.items():
        opponent_id = next(item for item in mechanism_ids if item != mechanism_id)
        opponent = provisional[(opponent_id, exit_clock)]
        advantage = float(row["stress_mean_return"]) - float(opponent["stress_mean_return"])
        feasible = bool(
            row["density_pass"]
            and float(row["stress_mean_return"]) > 0
            and float(row["stress_profit_factor"]) > 1
            and float(row["incremental_terminal_return_vs_static"]) > 0
            and int(row["positive_years"]) >= positive_years_required
            and advantage > 0
        )
        metric_rows.append(
            {
                **row,
                "opponent_mechanism_id": opponent_id,
                "stress_mean_advantage": advantage,
                "evidence": "FEASIBLE" if feasible else "DIRECTION_FAIL",
            }
        )

    return OpeningShockEvaluationResult(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        regime=pd.DataFrame(regime_rows),
        account=pd.DataFrame(account_rows),
    )
