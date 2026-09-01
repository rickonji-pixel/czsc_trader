"""Pure primitives for causal regime-conditioned factor weights."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from .four_layer import score_four_layer


def lagged_efficiency_ratio(close: pd.Series, lookback: int = 60) -> pd.Series:
    """Return path efficiency known before each session opens."""
    if int(lookback) < 2:
        raise ValueError("ER lookback must be at least two")
    known = close.astype(float).shift(1)
    net = known.sub(known.shift(int(lookback))).abs()
    path = known.diff().abs().rolling(int(lookback), min_periods=int(lookback)).sum()
    return net.div(path.where(path.gt(0.0))).rename("er60")


def fit_regime_threshold(
    er: pd.Series, start: pd.Timestamp, end: pd.Timestamp
) -> float:
    """Fit one unsupervised median threshold inside the declared research span."""
    sample = er.loc[pd.Timestamp(start) : pd.Timestamp(end)].dropna().astype(float)
    if sample.empty:
        raise ValueError("regime calibration has no valid ER observations")
    value = float(sample.median())
    if not np.isfinite(value):
        raise ValueError("regime threshold must be finite")
    return value


def classify_regimes(er: pd.Series, threshold: float) -> pd.Series:
    """Label finite observations as trend/range and missing observations as warmup."""
    if not np.isfinite(float(threshold)):
        raise ValueError("regime threshold must be finite")
    labels = pd.Series("warmup", index=er.index, dtype="string", name="regime")
    tied = pd.Series(
        np.isclose(er.astype(float), float(threshold), rtol=0.0, atol=1e-15),
        index=er.index,
    )
    trend = er.notna() & (er.ge(float(threshold)) | tied)
    labels.loc[er.notna() & ~trend] = "range"
    labels.loc[trend] = "trend"
    return labels


def project_group_weights(
    base_weights: pd.Series,
    groups: Mapping[str, Sequence[str]],
    trend_multiplier: float,
    volume_multiplier: float,
) -> pd.Series:
    """Apply two group multipliers while preserving identities, signs, and L1 scale."""
    required = ("structure", "trend", "volume_position")
    if any(name not in groups for name in required):
        raise ValueError("factor groups must contain structure, trend, and volume_position")
    membership = [str(item) for name in required for item in groups[name]]
    if len(membership) != len(set(membership)) or set(membership) != set(base_weights.index):
        raise ValueError("factor group membership differs from baseline weights")
    multipliers = {
        "structure": 1.0,
        "trend": float(trend_multiplier),
        "volume_position": float(volume_multiplier),
    }
    if any(not np.isfinite(value) or value <= 0.0 for value in multipliers.values()):
        raise ValueError("group multipliers must be positive finite values")
    projected = base_weights.astype(float).copy()
    for name in required:
        projected.loc[list(groups[name])] *= multipliers[name]
    scale = float(projected.abs().sum())
    if not np.isfinite(scale) or scale <= 0.0:
        raise ValueError("projected weights have invalid L1 scale")
    projected /= scale
    projected.name = "weight"
    return projected


def score_with_regime_weights(
    factors: pd.DataFrame,
    regimes: pd.Series,
    weights: Mapping[str, pd.Series],
    fallback: pd.Series,
) -> pd.Series:
    """Score each row with the weight vector selected by its causal regime label."""
    if not factors.index.equals(regimes.index):
        raise ValueError("factor and regime indices differ")
    if set(regimes.astype(str).unique()) - {"trend", "range", "warmup"}:
        raise ValueError("unknown regime label")
    if set(weights) != {"trend", "range"}:
        raise ValueError("regime weights must contain trend and range")
    output = pd.Series(index=factors.index, dtype=float, name="factor_score")
    choices = {"trend": weights["trend"], "range": weights["range"], "warmup": fallback}
    for label, selected in choices.items():
        mask = regimes.astype(str).eq(label)
        if mask.any():
            output.loc[mask] = score_four_layer(factors.loc[mask], selected)
    if output.isna().any():
        raise AssertionError("regime scoring left missing values")
    return output


def closed_trade_ledger(
    orders: pd.DataFrame, size_rtol: float = 1e-8
) -> pd.DataFrame:
    """Pair alternating full-position orders and ignore one open tail."""
    columns = (
        "entry_signal_date",
        "entry_date",
        "exit_signal_date",
        "exit_date",
        "size",
        "entry_price",
        "exit_price",
        "entry_fees",
        "exit_fees",
        "net_return",
    )
    if orders.empty:
        return pd.DataFrame(columns=columns)
    required = {"signal_date", "execution_date", "side", "size", "price", "fees"}
    if not required <= set(orders.columns):
        raise ValueError(f"orders missing columns: {sorted(required - set(orders.columns))}")
    records = orders.reset_index(drop=True)
    if str(records.iloc[0]["side"]).lower() != "buy":
        raise ValueError("closed trade order sequence must start with Buy")
    rows: list[dict[str, object]] = []
    pending: pd.Series | None = None
    for _, order in records.iterrows():
        side = str(order["side"]).lower()
        if side == "buy":
            if pending is not None:
                raise ValueError("closed trade order sequence contains consecutive Buy orders")
            pending = order
            continue
        if side != "sell":
            raise ValueError(f"unknown order side: {order['side']}")
        if pending is None:
            raise ValueError("closed trade order sequence contains Sell without Buy")
        buy_size = float(pending["size"])
        sell_size = float(order["size"])
        values = [buy_size, sell_size, pending["price"], order["price"], pending["fees"], order["fees"]]
        if not np.isfinite(np.asarray(values, dtype=float)).all():
            raise ValueError("closed trade order values must be finite")
        if not np.isclose(buy_size, sell_size, rtol=float(size_rtol), atol=1e-12):
            raise ValueError("closed trade buy and sell sizes differ")
        cost = buy_size * float(pending["price"]) + float(pending["fees"])
        proceeds = sell_size * float(order["price"]) - float(order["fees"])
        if cost <= 0.0:
            raise ValueError("closed trade entry cost must be positive")
        rows.append(
            {
                "entry_signal_date": pending["signal_date"],
                "entry_date": pending["execution_date"],
                "exit_signal_date": order["signal_date"],
                "exit_date": order["execution_date"],
                "size": buy_size,
                "entry_price": float(pending["price"]),
                "exit_price": float(order["price"]),
                "entry_fees": float(pending["fees"]),
                "exit_fees": float(order["fees"]),
                "net_return": proceeds / cost - 1.0,
            }
        )
        pending = None
    return pd.DataFrame(rows, columns=columns)


def risk_quality_metrics(
    equity: pd.Series, orders: pd.DataFrame, init_cash: float
) -> dict[str, float | int | bool]:
    """Calculate the three formal quality metrics and their support."""
    values = equity.astype(float)
    if values.empty or not np.isfinite(values.to_numpy()).all() or float(init_cash) <= 0.0:
        raise ValueError("equity and initial cash must be finite and non-empty")
    drawdown = values.div(values.cummax()).sub(1.0)
    max_drawdown = float(drawdown.min())
    total_return = float(values.iloc[-1] / float(init_cash) - 1.0)
    annualized_return = float((values.iloc[-1] / float(init_cash)) ** (252.0 / len(values)) - 1.0)
    calmar = (
        annualized_return / abs(max_drawdown)
        if np.isfinite(max_drawdown) and abs(max_drawdown) > 1e-12
        else float("nan")
    )
    ledger = closed_trade_ledger(orders)
    returns = ledger["net_return"].astype(float) if not ledger.empty else pd.Series(dtype=float)
    wins = returns.loc[returns.gt(0.0)]
    losses = returns.loc[returns.lt(0.0)]
    has_both = bool(not wins.empty and not losses.empty)
    win_loss_ratio = (
        float(wins.mean() / abs(losses.mean())) if has_both else float("nan")
    )
    return {
        "strategy_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "calmar": float(calmar),
        "win_loss_ratio": win_loss_ratio,
        "closed_trade_count": int(len(ledger)),
        "winning_trade_count": int(len(wins)),
        "losing_trade_count": int(len(losses)),
        "has_wins_and_losses": has_both,
    }


def strict_quality_pass(
    baseline: Mapping[str, object],
    challenger: Mapping[str, object],
    minimum_closed_trades: int,
    tolerance: float = 1e-12,
) -> bool:
    """Apply strict three-metric dominance and closed-trade support gates."""
    keys = ("max_drawdown", "calmar", "win_loss_ratio")
    values = [float(baseline[key]) for key in keys] + [float(challenger[key]) for key in keys]
    if not np.isfinite(values).all() or not bool(challenger.get("has_wins_and_losses", False)):
        return False
    required = max(
        int(minimum_closed_trades),
        int(np.ceil(0.5 * int(baseline["closed_trade_count"]))),
    )
    if int(challenger["closed_trade_count"]) < required:
        return False
    tol = float(tolerance)
    return bool(
        float(challenger["max_drawdown"]) > float(baseline["max_drawdown"]) + tol
        and float(challenger["calmar"]) > float(baseline["calmar"]) + tol
        and float(challenger["win_loss_ratio"]) > float(baseline["win_loss_ratio"]) + tol
    )


def candidate_relative_improvements(
    baseline: Mapping[str, object], challenger: Mapping[str, object]
) -> dict[str, float]:
    """Return dimensionless improvements used by the preregistered maximin rank."""
    base_drawdown = abs(float(baseline["max_drawdown"]))
    base_calmar = float(baseline["calmar"])
    base_ratio = float(baseline["win_loss_ratio"])
    if min(base_drawdown, abs(base_calmar), abs(base_ratio)) <= 1e-12:
        raise ValueError("baseline quality metrics cannot support relative ranking")
    return {
        "max_drawdown": (base_drawdown - abs(float(challenger["max_drawdown"]))) / base_drawdown,
        "calmar": (float(challenger["calmar"]) - base_calmar) / abs(base_calmar),
        "win_loss_ratio": (float(challenger["win_loss_ratio"]) - base_ratio) / abs(base_ratio),
    }
