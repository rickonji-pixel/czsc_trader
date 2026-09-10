"""Same-day rolling-inventory research under the A-share T+1 constraint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd

from .intraday_mechanism import (
    _matched_random_percentile,
    _performance,
    _rolling_episode_density,
)


@dataclass(frozen=True)
class InventoryEvaluationResult:
    """Episode, trade and account evidence for fixed inventory mechanisms."""

    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    regime: pd.DataFrame
    account: pd.DataFrame


def _same_day_outcomes(
    signal_times: pd.DatetimeIndex,
    five_minute: pd.DataFrame,
    *,
    exit_bars: int,
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
) -> pd.DataFrame:
    bars = five_minute.sort_values("Date").reset_index(drop=True)
    timestamps = pd.DatetimeIndex(pd.to_datetime(bars["Date"]))
    positions = timestamps.searchsorted(signal_times, side="right")
    rows: list[dict[str, object]] = []
    for signal_time, position in zip(signal_times, positions, strict=True):
        if position >= len(bars):
            continue
        entry = bars.iloc[int(position)]
        entry_date = pd.Timestamp(entry["Date"]).normalize()
        if entry_date != signal_time.normalize():
            continue
        exit_position = int(position) + exit_bars - 1
        if exit_position >= len(bars):
            continue
        exit_row = bars.iloc[exit_position]
        exit_date = pd.Timestamp(exit_row["Date"]).normalize()
        if exit_date != entry_date:
            continue
        entry_price = float(entry["Open"])
        exit_price = float(exit_row["Close"])
        rows.append(
            {
                "signal_time": signal_time,
                "signal_clock": signal_time.strftime("%H:%M"),
                "entry_time": pd.Timestamp(entry["Date"]),
                "entry_date": entry_date,
                "entry_price": entry_price,
                "exit_time": pd.Timestamp(exit_row["Date"]),
                "exit_date": exit_date,
                "exit_price": exit_price,
                "gross_return": exit_price / entry_price - 1.0,
                "baseline_return": (
                    exit_price * (1.0 - baseline_one_way_cost)
                    / (entry_price * (1.0 + baseline_one_way_cost))
                    - 1.0
                ),
                "stress_return": (
                    exit_price * (1.0 - stress_one_way_cost)
                    / (entry_price * (1.0 + stress_one_way_cost))
                    - 1.0
                ),
            }
        )
    return pd.DataFrame(rows)


def _account_metrics(
    episodes: pd.DataFrame,
    daily_close: pd.Series,
    *,
    base_fraction: float,
) -> dict[str, float]:
    close = daily_close.astype(float).sort_index()
    base = base_fraction * close.div(float(close.iloc[0]))
    trade_returns = pd.Series(0.0, index=close.index)
    realized = episodes.groupby("exit_date", observed=True)["stress_return"].sum()
    trade_returns.loc[trade_returns.index.intersection(realized.index)] = realized.reindex(
        trade_returns.index.intersection(realized.index)
    )
    cash = (1.0 - base_fraction) * (1.0 + trade_returns).cumprod()
    equity = base + cash
    static = base + (1.0 - base_fraction)
    full_buyhold = close.div(float(close.iloc[0]))
    years = max((close.index.max() - close.index.min()).days / 365.25, 1.0)

    def metrics(series: pd.Series) -> tuple[float, float, float]:
        drawdown = series.div(series.cummax()).sub(1.0)
        maximum_drawdown = float(drawdown.min())
        cagr = float((series.iloc[-1] / series.iloc[0]) ** (1.0 / years) - 1.0)
        calmar = cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else float("inf")
        return cagr, maximum_drawdown, calmar

    cagr, maximum_drawdown, calmar = metrics(equity)
    static_cagr, static_drawdown, static_calmar = metrics(static)
    buyhold_cagr, buyhold_drawdown, buyhold_calmar = metrics(full_buyhold)
    return {
        "terminal_equity": float(equity.iloc[-1]),
        "cagr": cagr,
        "max_drawdown": maximum_drawdown,
        "calmar": calmar,
        "static_terminal_equity": float(static.iloc[-1]),
        "static_cagr": static_cagr,
        "static_max_drawdown": static_drawdown,
        "static_calmar": static_calmar,
        "full_buyhold_terminal_equity": float(full_buyhold.iloc[-1]),
        "full_buyhold_cagr": buyhold_cagr,
        "full_buyhold_max_drawdown": buyhold_drawdown,
        "full_buyhold_calmar": buyhold_calmar,
        "incremental_terminal_return_vs_static": float(equity.iloc[-1] / static.iloc[-1] - 1.0),
    }


def evaluate_inventory_mechanisms(
    events: pd.DataFrame,
    fifteen_minute: pd.DataFrame,
    five_minute: pd.DataFrame,
    daily_regime: pd.Series,
    hypotheses: Sequence[Mapping[str, object]],
    *,
    evaluation_start: pd.Timestamp | str,
    exit_bars: Sequence[int],
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    base_fraction: float,
    density_window_sessions: int,
    median_episodes_required: int,
    p10_episodes_required: int,
    matched_random_simulations: int,
    random_seed: int,
) -> InventoryEvaluationResult:
    """Evaluate one buy-then-sell-old-inventory episode per signal day."""

    if not 0 < base_fraction < 1:
        raise ValueError("base_fraction must be between zero and one")
    event_source = events.copy()
    event_source["event_time"] = pd.to_datetime(event_source["event_time"])
    fifteen = fifteen_minute.copy()
    fifteen["Date"] = pd.to_datetime(fifteen["Date"])
    five = five_minute.copy()
    five["Date"] = pd.to_datetime(five["Date"])
    evaluation_start = pd.Timestamp(evaluation_start).normalize()
    calendar = pd.DatetimeIndex(
        sorted(five.loc[five["Date"] >= evaluation_start, "Date"].dt.normalize().unique()),
        name="date",
    )
    regimes = daily_regime.copy()
    regimes.index = pd.DatetimeIndex(pd.to_datetime(regimes.index)).normalize()
    regimes = regimes.astype("string")
    all_signal_times = pd.DatetimeIndex(
        fifteen.loc[fifteen["Date"] >= evaluation_start, "Date"].sort_values().unique()
    )
    daily_close = (
        five.loc[five["Date"].dt.strftime("%H:%M") == "15:00"]
        .assign(trade_date=lambda value: value["Date"].dt.normalize())
        .set_index("trade_date")["Close"]
        .reindex(calendar)
    )
    elapsed_years = max((calendar.max() - calendar.min()).days / 365.25, 1.0)
    episode_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    regime_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []

    for duration_number, bars_to_exit in enumerate(exit_bars):
        control = _same_day_outcomes(
            all_signal_times,
            five,
            exit_bars=int(bars_to_exit),
            baseline_one_way_cost=baseline_one_way_cost,
            stress_one_way_cost=stress_one_way_cost,
        )
        control["regime"] = regimes.reindex(control["signal_time"].dt.normalize()).to_numpy()
        for number, hypothesis in enumerate(hypotheses):
            selected = event_source.loc[
                event_source["name"].eq(str(hypothesis["signal_name"]))
                & event_source["state_primary"].eq(str(hypothesis["state_primary"]))
            ].sort_values("event_time")
            episodes = _same_day_outcomes(
                pd.DatetimeIndex(selected["event_time"]),
                five,
                exit_bars=int(bars_to_exit),
                baseline_one_way_cost=baseline_one_way_cost,
                stress_one_way_cost=stress_one_way_cost,
            )
            episodes = episodes.merge(
                selected[["event_time", "regime"]],
                left_on="signal_time",
                right_on="event_time",
                how="left",
                validate="one_to_one",
            ).drop(columns="event_time")
            if episodes["entry_date"].duplicated().any():
                raise ValueError("inventory evaluator allows at most one episode per day")
            episodes.insert(0, "hypothesis_id", str(hypothesis["hypothesis_id"]))
            episodes.insert(1, "exit_bars", int(bars_to_exit))
            episode_frames.append(episodes)
            median_count, p10_count, minimum_count, maximum_count = _rolling_episode_density(
                pd.DatetimeIndex(episodes["exit_date"]),
                calendar,
                density_window_sessions,
            )
            trade_metrics = _performance(episodes["stress_return"], elapsed_years)
            positive_years = 0
            for year, rows in episodes.groupby(episodes["exit_date"].dt.year, observed=True):
                item = _performance(rows["stress_return"], 1.0)
                annual_rows.append(
                    {
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "exit_bars": int(bars_to_exit),
                        "year": int(year),
                        **item,
                    }
                )
                positive_years += int(float(item["mean_return"]) > 0)
            for regime, rows in episodes.groupby("regime", observed=True):
                regime_rows.append(
                    {
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "exit_bars": int(bars_to_exit),
                        "regime": str(regime),
                        **_performance(rows["stress_return"], elapsed_years),
                    }
                )
            account = _account_metrics(
                episodes,
                daily_close,
                base_fraction=base_fraction,
            )
            account_rows.append(
                {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "exit_bars": int(bars_to_exit),
                    **account,
                }
            )
            density_pass = bool(
                median_count >= median_episodes_required
                and p10_count >= p10_episodes_required
            )
            percentile = _matched_random_percentile(
                episodes,
                control,
                simulations=matched_random_simulations,
                seed=random_seed + number * 100 + duration_number,
            )
            if not density_pass:
                evidence = "EVIDENCE_RATE_FAIL"
            elif (
                float(trade_metrics["mean_return"]) <= 0
                or float(trade_metrics["profit_factor"]) <= 1
                or float(account["incremental_terminal_return_vs_static"]) <= 0
            ):
                evidence = "DIRECTION_FAIL"
            elif percentile >= 0.90 and positive_years >= 4:
                evidence = "SUPPORTED"
            else:
                evidence = "MIXED"
            metric_rows.append(
                {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "mechanism": hypothesis["mechanism"],
                    "signal_name": hypothesis["signal_name"],
                    "state_primary": hypothesis["state_primary"],
                    "exit_bars": int(bars_to_exit),
                    "exit_trading_minutes": int(bars_to_exit) * 5,
                    **trade_metrics,
                    "positive_years": positive_years,
                    "rolling_30d_median_episodes": median_count,
                    "rolling_30d_p10_episodes": p10_count,
                    "rolling_30d_min_episodes": minimum_count,
                    "rolling_30d_max_episodes": maximum_count,
                    "density_pass": density_pass,
                    "matched_random_percentile": percentile,
                    "incremental_terminal_return_vs_static": account[
                        "incremental_terminal_return_vs_static"
                    ],
                    "evidence": evidence,
                }
            )
    return InventoryEvaluationResult(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        regime=pd.DataFrame(regime_rows),
        account=pd.DataFrame(account_rows),
    )
