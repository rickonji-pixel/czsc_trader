from __future__ import annotations

from hashlib import sha256

import pandas as pd

from czsc_trader.execution_intent import decide_order_intent

from .datasets import ReplayData
from .result import BacktestResult
from .signal_replay import SignalReplay


def _id(prefix: str, *parts: object) -> str:
    raw = "|".join(map(str, parts)).encode()
    return f"{prefix}-" + sha256(raw).hexdigest()[:20].upper()


def _frame(rows: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def replay_account(
    signal_replay: SignalReplay,
    replay_data: ReplayData,
    initial_cash: float,
) -> BacktestResult:
    """Replay a one-symbol cash/long account from confirmed virtual fills."""
    if initial_cash <= 0:
        raise ValueError("initial cash must be positive")
    spec = signal_replay.snapshot.resolved_rule.execution
    if spec is None:
        raise ValueError("strategy snapshot has no execution specification")
    daily = replay_data.execution_daily.set_index("dt").sort_index()
    intraday = replay_data.execution_intraday.set_index("dt").sort_index()
    evaluation = daily.loc[
        signal_replay.evaluation_start : signal_replay.evaluation_end
    ]
    decisions = signal_replay.decisions.copy()
    by_valid = decisions.dropna(subset=["valid_session"]).set_index("valid_session")
    cash = float(initial_cash)
    quantity = 0
    cycle_target: int | None = None
    cycle_id: str | None = None
    order_rows: list[dict[str, object]] = []
    fill_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []
    open_trade: dict[str, object] | None = None
    trade_rows: list[dict[str, object]] = []
    fee_rate = spec.capital.fee_rate

    for execution_date, price_row in evaluation.iterrows():
        if execution_date not in by_valid.index:
            raise ValueError(f"missing prior-session decision for {execution_date.date()}")
        decision = by_valid.loc[execution_date]
        if isinstance(decision, pd.DataFrame):
            raise ValueError(f"multiple decisions are valid for {execution_date.date()}")
        signal_date = pd.Timestamp(decision["signal_date"])
        target = int(decision["target_position"])
        cash_before = cash
        quantity_before = quantity
        if target == 1 and quantity == 0 and cycle_id is None:
            cycle_id = _id("CYC", signal_replay.snapshot.identity.reference, signal_date)
        intent = decide_order_intent(
            target_position=target,
            actual_quantity=quantity,
            cycle_target_quantity=cycle_target,
            available_cash=cash,
            execution_close=float(daily.loc[signal_date, "close"]),
            execution_spec=spec,
        )
        if target == 1 and cycle_target is None:
            cycle_target = intent.cycle_target_quantity
        day_bars = intraday.loc[intraday.index.normalize() == execution_date.normalize()]
        exit_proceeds = 0.0
        exit_fees = 0.0
        exit_time: pd.Timestamp | None = None
        exit_quantity = 0
        for slice_number, order in enumerate(intent.orders, start=1):
            order_id = _id("ORD", decision["decision_id"], execution_date, slice_number)
            trigger: str | None = None
            fill_price: float | None = None
            fill_time: pd.Timestamp | None = None
            if order.side == "BUY":
                if float(price_row["open"]) <= order.limit_price:
                    trigger = "OPEN"
                    fill_price = float(price_row["open"])
                    fill_time = execution_date
                else:
                    touches = day_bars.loc[day_bars["low"].astype(float).lt(order.limit_price)]
                    if not touches.empty:
                        trigger = "INTRADAY_LIMIT"
                        fill_price = order.limit_price
                        fill_time = pd.Timestamp(touches.index[0])
                required = (
                    order.quantity * fill_price * (1.0 + fee_rate)
                    if fill_price is not None
                    else None
                )
                if required is not None and required > cash + 1e-8:
                    trigger = None
                    fill_price = None
                    fill_time = None
            elif order.side == "SELL":
                if float(price_row["open"]) >= order.limit_price:
                    trigger = "OPEN"
                    fill_price = float(price_row["open"])
                    fill_time = execution_date
                else:
                    touches = day_bars.loc[day_bars["high"].astype(float).gt(order.limit_price)]
                    if not touches.empty:
                        trigger = "INTRADAY_LIMIT"
                        fill_price = order.limit_price
                        fill_time = pd.Timestamp(touches.index[0])
            else:
                raise ValueError(f"unsupported order side: {order.side}")
            status = "FILLED" if fill_price is not None else "UNFILLED"
            order_rows.append(
                {
                    "order_id": order_id,
                    "decision_id": decision["decision_id"],
                    "cycle_id": cycle_id,
                    "signal_date": signal_date,
                    "execution_date": execution_date,
                    "side": order.side,
                    "quantity": order.quantity,
                    "limit_price": order.limit_price,
                    "status": status,
                }
            )
            if fill_price is None or fill_time is None:
                continue
            gross = order.quantity * fill_price
            fees = gross * fee_rate
            if order.side == "BUY":
                cash -= gross + fees
                quantity += order.quantity
            else:
                cash += gross - fees
                quantity -= order.quantity
            fill = {
                "fill_id": _id("FIL", order_id, fill_time),
                "order_id": order_id,
                "decision_id": decision["decision_id"],
                "cycle_id": cycle_id,
                "signal_date": signal_date,
                "fill_time": fill_time,
                "side": order.side,
                "quantity": order.quantity,
                "price": fill_price,
                "fees": fees,
                "trigger": trigger,
            }
            fill_rows.append(fill)
            if order.side == "BUY":
                if open_trade is None:
                    open_trade = {
                        "cycle_id": cycle_id,
                        "entry_date": fill_time,
                        "quantity": 0,
                        "gross": 0.0,
                        "fees": 0.0,
                    }
                open_trade["quantity"] = int(open_trade["quantity"]) + order.quantity
                open_trade["gross"] = float(open_trade["gross"]) + gross
                open_trade["fees"] = float(open_trade["fees"]) + fees
            else:
                exit_proceeds += gross - fees
                exit_fees += fees
                exit_time = fill_time
                exit_quantity += order.quantity
        if exit_quantity and quantity == 0 and open_trade is not None and exit_time is not None:
            entry_quantity = int(open_trade["quantity"])
            entry_gross = float(open_trade["gross"])
            entry_cost = entry_gross + float(open_trade["fees"])
            trade_rows.append(
                {
                    "cycle_id": cycle_id,
                    "status": "CLOSED",
                    "entry_date": open_trade["entry_date"],
                    "exit_date": exit_time,
                    "quantity": entry_quantity,
                    "entry_price": entry_gross / entry_quantity,
                    "exit_price": (exit_proceeds + exit_fees) / exit_quantity,
                    "net_return": exit_proceeds / entry_cost - 1.0,
                }
            )
            open_trade = None
        if quantity > 0:
            cycle_target = quantity
        if target == 0 and quantity == 0:
            cycle_target = None
            cycle_id = None
        account_rows.append(
            {
                "date": execution_date,
                "signal_date": signal_date,
                "target_position": target,
                "cash_before": cash_before,
                "quantity_before": quantity_before,
                "cash": cash,
                "quantity": quantity,
                "close": float(price_row["close"]),
                "equity": cash + quantity * float(price_row["close"]),
            }
        )
    if open_trade is not None:
        entry_quantity = int(open_trade["quantity"])
        entry_gross = float(open_trade["gross"])
        trade_rows.append(
            {
                "cycle_id": open_trade["cycle_id"],
                "status": "OPEN",
                "entry_date": open_trade["entry_date"],
                "exit_date": pd.NaT,
                "quantity": entry_quantity,
                "entry_price": entry_gross / entry_quantity,
                "exit_price": float("nan"),
                "net_return": float("nan"),
            }
        )
    return BacktestResult(
        identity=signal_replay.snapshot.identity,
        decisions=decisions,
        orders=_frame(
            order_rows,
            [
                "order_id", "decision_id", "cycle_id", "signal_date",
                "execution_date", "side", "quantity", "limit_price", "status",
            ],
        ),
        fills=_frame(
            fill_rows,
            [
                "fill_id", "order_id", "decision_id", "cycle_id", "signal_date",
                "fill_time", "side", "quantity", "price", "fees", "trigger",
            ],
        ),
        account_daily=pd.DataFrame(account_rows),
        trades=_frame(
            trade_rows,
            [
                "cycle_id", "status", "entry_date", "exit_date", "quantity",
                "entry_price", "exit_price", "net_return",
            ],
        ),
    )
