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
class FixedSelectionResult:
    """One fixed CZSC factor rule and its fully auditable decisions."""

    target_position: pd.Series
    scores: pd.Series
    events: pd.DataFrame
    candidates: pd.DataFrame
    rule: Rule


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


def build_factor_events(
    target_position: pd.Series,
    scores: pd.Series,
    factors: pd.DataFrame,
    rule: Rule,
) -> pd.DataFrame:
    """Describe every position transition using only its CZSC factor snapshot."""
    target = target_position.astype(float)
    previous = target.shift(1, fill_value=0.0)
    rows: list[dict[str, object]] = []
    for signal_date in target.index[target.ne(previous)]:
        after = float(target.loc[signal_date])
        event_type = "Entry" if after == 1.0 else "Exit"
        score = float(scores.loc[signal_date])
        rows.append(
            {
                "event_id": f"Factor:{pd.Timestamp(signal_date):%Y%m%d}:{event_type}",
                "signal_date": pd.Timestamp(signal_date),
                "event_type": event_type,
                "structure": float(factors.loc[signal_date, "structure"]),
                "trend": float(factors.loc[signal_date, "trend"]),
                "volume_position": float(factors.loc[signal_date, "volume_position"]),
                "factor_score": score,
                "enter_threshold": float(rule.enter),
                "exit_threshold": float(rule.exit),
                "before_position": float(previous.loc[signal_date]),
                "after_position": after,
                "reason": (
                    f"score {score:.6f} >= enter {rule.enter:.6f}"
                    if event_type == "Entry"
                    else f"score {score:.6f} <= exit {rule.exit:.6f}"
                ),
            }
        )
    return pd.DataFrame(rows)


def _normalize_daily(daily: pd.DataFrame) -> pd.DataFrame:
    prices = daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    return prices.sort_index()


def _period_candidate_metrics(
    prices: pd.DataFrame,
    target: pd.Series,
    requested_start: pd.Timestamp,
    requested_end: pd.Timestamp,
    fee_rate: float,
) -> dict[str, float | int]:
    index = prices.index[
        (prices.index >= pd.Timestamp(requested_start))
        & (prices.index <= pd.Timestamp(requested_end))
    ]
    if len(index) == 0:
        raise ValueError("candidate period contains no prices")
    prior = prices.index[prices.index < index[0]]
    if len(prior) == 0:
        raise ValueError("candidate period has no prior factor signal")
    execution = target.loc[index].shift(1)
    execution.iloc[0] = float(target.loc[prior[-1]])
    cash = 1.0
    shares = 0.0
    previous_target = 0.0
    values: list[float] = []
    changes = 0
    for dt in index:
        desired = float(execution.loc[dt])
        open_price = float(prices.loc[dt, "open"])
        if desired != previous_target:
            changes += 1
            if desired == 1.0:
                shares = cash / (open_price * (1.0 + fee_rate))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee_rate)
                shares = 0.0
            previous_target = desired
        values.append(cash + shares * float(prices.loc[dt, "close"]))
    curve = pd.Series(values, index=index)
    strategy_return = float(curve.iloc[-1] - 1.0)
    buyhold_return = float(
        float(prices.loc[index[-1], "close"])
        / (float(prices.loc[index[0], "open"]) * (1.0 + fee_rate))
        - 1.0
    )
    return {
        "strategy_return": strategy_return,
        "buyhold_return": buyhold_return,
        "excess_return": strategy_return - buyhold_return,
        "drawdown": float((curve / curve.cummax() - 1.0).min()),
        "changes": changes,
        "days": len(index),
    }


def rank_candidate_results(candidates: pd.DataFrame) -> pd.DataFrame:
    """Rank fixed rules by cross-period robustness before secondary metrics."""
    return candidates.sort_values(
        ["pass_count", "min_excess", "mean_excess", "turnover", "max_drawdown", "complexity", "rule_id"],
        ascending=[False, False, False, True, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def _rule_id(rule: Rule) -> str:
    weights = "-".join(f"{value:.2f}" for value in rule.weights)
    return f"w{weights}_en{rule.enter:.2f}_ex{rule.exit:.2f}_c{rule.confirm_days}_h{rule.min_hold_days}"


def select_fixed_rule(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    fee_rate: float = 0.0005,
) -> FixedSelectionResult:
    """Select one fixed CZSC-only rule using the declared target-period objective."""
    prices = _normalize_daily(daily)
    aligned = factors.loc[:, FACTOR_COLUMNS].reindex(prices.index).fillna(0.0)
    rows: list[dict[str, object]] = []
    targets: dict[str, tuple[Rule, pd.Series, pd.Series]] = {}
    for rule in CANDIDATES:
        target, scores = positions_for_rule(aligned, rule)
        rule_id = _rule_id(rule)
        targets[rule_id] = (rule, target, scores)
        metrics = {
            name: _period_candidate_metrics(prices, target, start, end, fee_rate)
            for name, (start, end) in periods.items()
        }
        excesses = [float(item["excess_return"]) for item in metrics.values()]
        row: dict[str, object] = {
            "rule_id": rule_id,
            "weights": "|".join(f"{value:.2f}" for value in rule.weights),
            "enter": rule.enter,
            "exit": rule.exit,
            "confirm_days": rule.confirm_days,
            "min_hold_days": rule.min_hold_days,
            "pass_count": sum(value > 0.0 for value in excesses),
            "min_excess": min(excesses),
            "mean_excess": float(np.mean(excesses)),
            "turnover": sum(int(item["changes"]) for item in metrics.values())
            / max(sum(int(item["days"]) for item in metrics.values()), 1),
            "max_drawdown": min(float(item["drawdown"]) for item in metrics.values()),
            "complexity": rule.confirm_days + rule.min_hold_days,
        }
        for name, item in metrics.items():
            row[f"{name}_strategy_return"] = item["strategy_return"]
            row[f"{name}_buyhold_return"] = item["buyhold_return"]
            row[f"{name}_excess_return"] = item["excess_return"]
        rows.append(row)
    ranked = rank_candidate_results(pd.DataFrame(rows))
    selected_rule, target, scores = targets[str(ranked.iloc[0]["rule_id"])]
    events = build_factor_events(target, scores, aligned, selected_rule)
    return FixedSelectionResult(target, scores, events, ranked, selected_rule)


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


