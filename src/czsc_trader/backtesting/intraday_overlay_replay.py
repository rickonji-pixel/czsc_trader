from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import resolve_repository_experiment_reference

from .datasets import ReplayData
from .models import StrategySnapshot
from .result import BacktestResult
from .signal_replay import SignalReplay


def _id(prefix: str, *parts: object) -> str:
    raw = "|".join(map(str, parts)).encode()
    return f"{prefix}-" + sha256(raw).hexdigest()[:20].upper()


def _frame(rows: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def build_moneyflow_breadth_signals(
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    repository_root: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> SignalReplay:
    """Build causal S003 decisions from immutable historical-constituent evidence."""
    spec = snapshot.resolved_rule.constituent_moneyflow_intraday
    if spec is None:
        raise ValueError("strategy snapshot has no constituent-moneyflow intraday specification")
    source = resolve_repository_experiment_reference(repository_root, spec.source_path)
    panel = pd.read_csv(source)
    required = {"dt", "con_code", "weight", "net_mf_amount", "observed_moneyflow"}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"constituent-moneyflow panel missing columns {missing}")
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel = panel.loc[panel["dt"] <= pd.Timestamp(replay_data.cutoff)].copy()
    if panel.duplicated(["dt", "con_code"]).any():
        raise ValueError("constituent-moneyflow panel contains duplicate member sessions")
    panel["weight"] = pd.to_numeric(panel["weight"], errors="raise")
    panel["net_mf_amount"] = pd.to_numeric(panel["net_mf_amount"], errors="coerce")
    observed = panel["observed_moneyflow"].astype(str).str.lower().map(
        {"true": True, "false": False, "1": True, "0": False}
    )
    if observed.isna().any():
        raise ValueError("constituent-moneyflow observed flag is invalid")
    panel["observed_weight"] = panel["weight"].where(observed, 0.0)
    panel["positive_weight"] = panel["weight"].where(panel["net_mf_amount"].gt(0), 0.0)
    daily = panel.groupby("dt", sort=True, observed=True).agg(
        total_weight=("weight", "sum"),
        observed_weight=("observed_weight", "sum"),
        positive_weight=("positive_weight", "sum"),
    )
    daily["observed_weight_ratio"] = daily["observed_weight"] / daily["total_weight"]
    daily["moneyflow_breadth"] = daily["positive_weight"] / daily["observed_weight"]
    daily.loc[
        daily["observed_weight_ratio"].lt(spec.minimum_observed_weight_ratio),
        "moneyflow_breadth",
    ] = pd.NA
    daily["threshold"] = (
        daily["moneyflow_breadth"]
        .shift(1)
        .rolling(
            spec.threshold_lookback_sessions,
            min_periods=spec.threshold_lookback_sessions,
        )
        .quantile(spec.threshold_quantile)
    )

    sessions = pd.DatetimeIndex(
        pd.to_datetime(replay_data.adjusted.daily["dt"]).dt.normalize(), name="dt"
    )
    next_session = pd.Series(sessions[1:], index=sessions[:-1])
    selected = daily.loc[daily["moneyflow_breadth"].ge(daily["threshold"])].copy()
    selected["valid_session"] = selected.index.map(next_session)
    selected = selected.loc[selected["valid_session"].notna()].copy()
    evaluation = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    if evaluation.empty:
        raise ValueError("backtest interval contains no trading sessions")
    selected = selected.loc[selected["valid_session"].isin(evaluation)]
    if selected["valid_session"].duplicated().any():
        raise ValueError("constituent-moneyflow generated more than one event per day")
    rows = []
    for signal_date, row in selected.iterrows():
        rows.append(
            {
                "decision_id": _id("DEC", snapshot.identity.reference, signal_date, "INTRADAY_LONG"),
                "signal_date": pd.Timestamp(signal_date),
                "valid_session": pd.Timestamp(row["valid_session"]),
                "target_position": 1,
                "factor_score": float(row["moneyflow_breadth"]),
                "regime": None,
                "threshold": float(row["threshold"]),
                "observed_weight_ratio": float(row["observed_weight_ratio"]),
                "action": "INTRADAY_LONG_OVERLAY",
            }
        )
    return SignalReplay(
        snapshot=snapshot,
        decisions=pd.DataFrame(rows),
        calculation_start=min(sessions[0], daily.index.min()),
        calculation_end=max(sessions[-1], daily.index.max()),
        evaluation_start=evaluation[0],
        evaluation_end=evaluation[-1],
    )


def replay_intraday_overlay(
    signals: SignalReplay,
    replay_data: ReplayData,
    initial_cash: float,
) -> BacktestResult:
    """Replay a sellable core plus same-day event rotation without violating T+1."""
    if initial_cash <= 0:
        raise ValueError("initial cash must be positive")
    spec = signals.snapshot.resolved_rule.constituent_moneyflow_intraday
    if spec is None:
        raise ValueError("strategy snapshot has no constituent-moneyflow intraday specification")
    five = replay_data.execution_five_minute
    if five is None:
        raise ValueError("intraday overlay replay requires unadjusted 5m execution data")
    bars = five.copy()
    bars["date"] = pd.to_datetime(bars["dt"]).dt.normalize()
    bars["clock"] = pd.to_datetime(bars["dt"]).dt.strftime("%H:%M")
    opening = bars.loc[bars["clock"].eq("09:35")].set_index("date")["open"].astype(float)
    exit_price = bars.loc[bars["clock"].eq("11:30")].set_index("date")["close"].astype(float)
    daily = replay_data.execution_daily.set_index("dt").sort_index()
    evaluation = daily.loc[signals.evaluation_start : signals.evaluation_end]
    if evaluation.empty or not evaluation.index.isin(opening.index).all() or not evaluation.index.isin(exit_price.index).all():
        raise ValueError("intraday overlay replay has incomplete OPEN or 11:30 checkpoints")
    prior = daily.loc[daily.index < signals.evaluation_start]
    if prior.empty:
        raise ValueError("intraday overlay replay requires one pre-window session for sellable core")
    core_price = float(prior.iloc[-1]["close"])
    lot = spec.lot_size
    core_budget = initial_cash * spec.core_fraction
    quantity = int(core_budget / (core_price * (1.0 + spec.one_way_cost)) // lot * lot)
    if quantity <= 0:
        raise ValueError("initial cash cannot establish one sellable core lot")
    cash = float(initial_cash - quantity * core_price * (1.0 + spec.one_way_cost))
    event_by_date = signals.decisions.set_index("valid_session") if not signals.decisions.empty else pd.DataFrame()
    order_rows: list[dict[str, object]] = []
    fill_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    for day, price_row in evaluation.iterrows():
        cash_before = cash
        quantity_before = quantity
        signal_date = pd.NaT
        if not event_by_date.empty and day in event_by_date.index:
            decision = event_by_date.loc[day]
            if isinstance(decision, pd.DataFrame):
                raise ValueError(f"multiple intraday decisions are valid for {day.date()}")
            signal_date = pd.Timestamp(decision["signal_date"])
            buy_price = float(opening.loc[day])
            sell_price = float(exit_price.loc[day])
            affordable = int(cash / (buy_price * (1.0 + spec.one_way_cost)) // lot * lot)
            trade_quantity = min(affordable, quantity)
            if trade_quantity < lot:
                raise ValueError(f"insufficient event cash or sellable core inventory on {day.date()}")
            cycle_id = _id("CYC", decision["decision_id"], day)
            buy_order_id = _id("ORD", cycle_id, "BUY")
            sell_order_id = _id("ORD", cycle_id, "SELL")
            buy_fees = trade_quantity * buy_price * spec.one_way_cost
            sell_fees = trade_quantity * sell_price * spec.one_way_cost
            cash -= trade_quantity * buy_price + buy_fees
            quantity += trade_quantity
            cash += trade_quantity * sell_price - sell_fees
            quantity -= trade_quantity
            for order_id, side, price, checkpoint in (
                (buy_order_id, "BUY", buy_price, "OPEN"),
                (sell_order_id, "SELL", sell_price, "11:30_CLOSE"),
            ):
                order_rows.append(
                    {
                        "order_id": order_id,
                        "decision_id": decision["decision_id"],
                        "cycle_id": cycle_id,
                        "signal_date": signal_date,
                        "execution_date": day,
                        "side": side,
                        "quantity": trade_quantity,
                        "order_type": "MARKETABLE_LIMIT",
                        "limit_price": price,
                        "status": "FILLED",
                        "checkpoint": checkpoint,
                    }
                )
            for order_id, side, price, fees, checkpoint in (
                (buy_order_id, "BUY", buy_price, buy_fees, "OPEN"),
                (sell_order_id, "SELL", sell_price, sell_fees, "11:30_CLOSE"),
            ):
                fill_rows.append(
                    {
                        "fill_id": _id("FIL", order_id, checkpoint),
                        "order_id": order_id,
                        "decision_id": decision["decision_id"],
                        "cycle_id": cycle_id,
                        "signal_date": signal_date,
                        "fill_time": (
                            pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=30)
                            if side == "BUY"
                            else pd.Timestamp(day) + pd.Timedelta(hours=11, minutes=30)
                        ),
                        "side": side,
                        "quantity": trade_quantity,
                        "price": price,
                        "fees": fees,
                        "trigger": checkpoint,
                    }
                )
            trade_rows.append(
                {
                    "cycle_id": cycle_id,
                    "status": "CLOSED",
                    "entry_date": pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=30),
                    "exit_date": pd.Timestamp(day) + pd.Timedelta(hours=11, minutes=30),
                    "quantity": trade_quantity,
                    "entry_price": buy_price,
                    "exit_price": sell_price,
                    "net_return": sell_price * (1.0 - spec.one_way_cost) / (
                        buy_price * (1.0 + spec.one_way_cost)
                    ) - 1.0,
                }
            )
        account_rows.append(
            {
                "date": day,
                "signal_date": signal_date,
                "target_position": spec.core_fraction,
                "cash_before": cash_before,
                "quantity_before": quantity_before,
                "cash": cash,
                "quantity": quantity,
                "close": float(price_row["close"]),
                "equity": cash + quantity * float(price_row["close"]),
            }
        )
    return BacktestResult(
        identity=signals.snapshot.identity,
        decisions=signals.decisions,
        orders=_frame(order_rows, [
            "order_id", "decision_id", "cycle_id", "signal_date", "execution_date",
            "side", "quantity", "order_type", "limit_price", "status", "checkpoint",
        ]),
        fills=_frame(fill_rows, [
            "fill_id", "order_id", "decision_id", "cycle_id", "signal_date", "fill_time",
            "side", "quantity", "price", "fees", "trigger",
        ]),
        account_daily=pd.DataFrame(account_rows),
        trades=_frame(trade_rows, [
            "cycle_id", "status", "entry_date", "exit_date", "quantity", "entry_price",
            "exit_price", "net_return",
        ]),
    )
