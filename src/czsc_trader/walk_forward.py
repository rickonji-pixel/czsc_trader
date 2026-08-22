"""Monthly walk-forward rule selection with strict information cutoffs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product

import numpy as np
import pandas as pd


FACTOR_COLUMNS = ("structure", "trend", "volume_position")
MIN_TRAIN_DAYS = 120
MAX_TRAIN_DAYS = 252


@dataclass(frozen=True)
class Rule:
    """A transparent score threshold and position-state rule."""

    weights: tuple[float, float, float]
    enter: float
    exit: float
    confirm_days: int
    min_hold_days: int


DEFAULT_RULE = Rule((0.4, 0.4, 0.2), enter=0.2, exit=0.0, confirm_days=1, min_hold_days=3)
CANDIDATES = tuple(
    Rule(weights, enter, exit_, confirm, hold)
    for weights, enter, exit_, confirm, hold in product(
        ((0.5, 0.3, 0.2), (0.4, 0.4, 0.2), (0.4, 0.3, 0.3)),
        (0.05, 0.20, 0.35),
        (-0.20, 0.0, 0.10),
        (1, 2),
        (1, 3, 5),
    )
    if exit_ < enter
)


@dataclass(frozen=True)
class WalkForwardResult:
    target_position: pd.Series
    scores: pd.Series
    selections: pd.DataFrame


@dataclass(frozen=True)
class AlphaLockResult:
    """Target positions after causal benchmark-alpha preservation."""

    target_position: pd.Series
    events: pd.DataFrame


def positions_for_rule(factors: pd.DataFrame, rule: Rule) -> tuple[pd.Series, pd.Series]:
    """Apply a rule as a deterministic state machine without using prices."""
    clean = factors.loc[:, FACTOR_COLUMNS].fillna(0.0).astype(float)
    scores = clean.mul(np.asarray(rule.weights), axis=1).sum(axis=1)
    positions: list[float] = []
    position = 0.0
    confirmations = 0
    holding_days = 0
    for score in scores:
        if position == 0.0:
            confirmations = confirmations + 1 if score >= rule.enter else 0
            if confirmations >= rule.confirm_days:
                position = 1.0
                holding_days = 1
                confirmations = 0
        else:
            if score <= rule.exit and holding_days >= rule.min_hold_days:
                position = 0.0
                holding_days = 0
                confirmations = 0
            else:
                holding_days += 1
        positions.append(position)
    return (
        pd.Series(positions, index=factors.index, name="target_position", dtype=float),
        pd.Series(scores, index=factors.index, name="factor_score", dtype=float),
    )


def _simulate_rule(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    rule: Rule,
    fee_rate: float,
) -> tuple[float, float, float]:
    """Return net return, maximum drawdown, and turnover rate on completed history."""
    target, _ = positions_for_rule(factors, rule)
    execution_target = target.shift(1).fillna(0.0)
    cash = 1.0
    shares = 0.0
    previous_target = 0.0
    equity: list[float] = []
    changes = 0
    for date, row in daily.iterrows():
        desired = float(execution_target.loc[date])
        open_price = float(row["open"])
        close_price = float(row["close"])
        if desired != previous_target:
            changes += 1
            if desired == 1.0:
                shares = cash / (open_price * (1.0 + fee_rate))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee_rate)
                shares = 0.0
            previous_target = desired
        equity.append(cash + shares * close_price)
    curve = pd.Series(equity, index=daily.index)
    net_return = float(curve.iloc[-1] / curve.iloc[0] - 1.0)
    drawdown = float((curve / curve.cummax() - 1.0).min())
    turnover = changes / max(len(daily), 1)
    return net_return, drawdown, turnover


def _buyhold_stats(daily: pd.DataFrame, fee_rate: float) -> tuple[float, float]:
    shares = 1.0 / (float(daily.iloc[0]["open"]) * (1.0 + fee_rate))
    curve = daily["close"].astype(float) * shares
    return float(curve.iloc[-1] / curve.iloc[0] - 1.0), float((curve / curve.cummax() - 1.0).min())


def _rule_distance(left: Rule, right: Rule) -> float:
    return float(
        sum(abs(a - b) for a, b in zip(left.weights, right.weights, strict=True))
        + abs(left.enter - right.enter)
        + abs(left.exit - right.exit)
        + abs(left.confirm_days - right.confirm_days)
        + abs(left.min_hold_days - right.min_hold_days)
    )


def _choose_rule(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    previous_rule: Rule,
    fee_rate: float,
) -> tuple[Rule, float]:
    buyhold_return, buyhold_drawdown = _buyhold_stats(daily, fee_rate)
    ranked: list[tuple[float, float, float, int, Rule]] = []
    for order, rule in enumerate(CANDIDATES):
        net_return, drawdown, turnover = _simulate_rule(daily, factors, rule, fee_rate)
        objective = (
            net_return
            - buyhold_return
            - 0.5 * max(0.0, abs(drawdown) - abs(buyhold_drawdown))
            - 0.10 * turnover
        )
        ranked.append((objective, -turnover, -_rule_distance(rule, previous_rule), -order, rule))
    best = max(ranked, key=lambda item: item[:4])
    return best[4], float(best[0])


def _selection_row(
    as_of_date: pd.Timestamp,
    train_index: pd.DatetimeIndex,
    rule: Rule,
    objective: float | None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "as_of_date": as_of_date,
        "train_start": train_index.min() if len(train_index) else pd.NaT,
        "train_end": train_index.max() if len(train_index) else pd.NaT,
        "objective": objective,
        "candidate_count": len(CANDIDATES),
        "weights": "|".join(f"{weight:.1f}" for weight in rule.weights),
    }
    row.update({key: value for key, value in asdict(rule).items() if key != "weights"})
    return row


def run_walk_forward(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    fee_rate: float = 0.0005,
) -> WalkForwardResult:
    """Select rules monthly using prior rows only and return daily decisions."""
    prices = daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    prices = prices.sort_index()
    aligned_factors = factors.loc[:, FACTOR_COLUMNS].reindex(prices.index).fillna(0.0)
    month_starts = prices.groupby(prices.index.to_period("M")).head(1).index

    target = pd.Series(0.0, index=prices.index, name="target_position")
    scores = pd.Series(0.0, index=prices.index, name="factor_score")
    selections: list[dict[str, object]] = []
    previous_rule = DEFAULT_RULE

    for month_number, as_of_date in enumerate(month_starts):
        prior_index = prices.index[prices.index < as_of_date][-MAX_TRAIN_DAYS:]
        if len(prior_index) >= MIN_TRAIN_DAYS:
            selected, objective = _choose_rule(
                prices.loc[prior_index],
                aligned_factors.loc[prior_index],
                previous_rule,
                fee_rate,
            )
            train_index = prior_index
        else:
            selected, objective = DEFAULT_RULE, None
            train_index = pd.DatetimeIndex([], name="dt")

        next_start = month_starts[month_number + 1] if month_number + 1 < len(month_starts) else None
        month_mask = prices.index >= as_of_date
        if next_start is not None:
            month_mask &= prices.index < next_start
        selected_positions, selected_scores = positions_for_rule(aligned_factors.loc[:], selected)
        target.loc[month_mask] = selected_positions.loc[month_mask]
        scores.loc[month_mask] = selected_scores.loc[month_mask]
        selections.append(_selection_row(as_of_date, train_index, selected, objective))
        previous_rule = selected

    return WalkForwardResult(target, scores, pd.DataFrame(selections))


def apply_annual_alpha_lock(
    daily: pd.DataFrame,
    base_target: pd.Series,
    base_equity: pd.Series,
    *,
    min_history: int = 252,
) -> AlphaLockResult:
    """Lock in positive Q1 benchmark alpha by tracking the asset through year-end.

    The decision is stamped on the final completed Q1 session, so the changed
    target can only execute at the following session's open.
    """
    prices = daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    prices = prices.sort_index()
    target = base_target.reindex(prices.index).astype(float).copy()
    equity = base_equity.reindex(prices.index).astype(float)
    events: list[dict[str, object]] = []

    for year in sorted(prices.index.year.unique()):
        year_start = pd.Timestamp(year=year, month=1, day=1)
        prior_dates = prices.index[prices.index < year_start]
        if len(prior_dates) < min_history:
            continue
        anchor = prior_dates[-1]
        q1_dates = prices.index[
            (prices.index.year == year)
            & (prices.index <= pd.Timestamp(year=year, month=3, day=31))
        ]
        if len(q1_dates) == 0:
            continue
        q1_end = q1_dates[-1]
        strategy_return = float(equity.loc[q1_end] / equity.loc[anchor] - 1.0)
        buyhold_return = float(prices.loc[q1_end, "close"] / prices.loc[anchor, "close"] - 1.0)
        excess = strategy_return - buyhold_return
        if excess <= 0.0:
            continue
        lock_mask = (prices.index >= q1_end) & (prices.index.year == year)
        target.loc[lock_mask] = 1.0
        events.append(
            {
                "decision_date": q1_end,
                "lock_until": prices.index[prices.index.year == year][-1],
                "history_days": len(prior_dates),
                "q1_strategy_return": strategy_return,
                "q1_buyhold_return": buyhold_return,
                "q1_excess_return": excess,
            }
        )
    return AlphaLockResult(target.rename("target_position"), pd.DataFrame(events))
