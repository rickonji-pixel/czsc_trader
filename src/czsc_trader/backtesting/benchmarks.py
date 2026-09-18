from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from czsc_trader.moving_average import moving_average_signals
from czsc_trader.research_backtest import run_backtest as run_next_open_backtest
from czsc_trader.strategy_metrics import closed_trade_ledger, strategy_comparison_metrics

from .datasets import ReplayData
from .signal_replay import SignalReplay


@dataclass(frozen=True)
class BenchmarkReplay:
    metrics: dict[str, object]
    buyhold_account_daily: pd.DataFrame
    ma_signals: pd.DataFrame
    ma_orders: pd.DataFrame
    ma_account_daily: pd.DataFrame
    ma_trades: pd.DataFrame


def _fee_rate(signals: SignalReplay) -> float:
    support = signals.support_data or {}
    if support.get("mode") == "srt_input_contract":
        policy = support.get("execution_policy")
        if not isinstance(policy, dict):
            raise ValueError("SRT replay has no execution policy evidence")
        settings = policy.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("SRT replay execution settings are invalid")
        policy_type = policy.get("policy_type")
        if policy_type == "FROZEN_RULE":
            capital = settings.get("capital")
            if not isinstance(capital, dict) or "fee_rate" not in capital:
                raise ValueError("SRT frozen rule has no fee rate")
            return float(capital["fee_rate"])
        if policy_type == "INTRADAY_OVERLAY" and "one_way_cost" in settings:
            return float(settings["one_way_cost"])
        raise ValueError(f"unsupported SRT execution policy: {policy_type}")
    resolved = signals.snapshot.resolved_rule
    if resolved is None:
        raise ValueError("candidate benchmark requires a resolved research rule")
    if resolved.execution is not None:
        return float(resolved.execution.capital.fee_rate)
    if resolved.constituent_moneyflow_intraday is not None:
        return float(resolved.constituent_moneyflow_intraday.one_way_cost)
    raise ValueError("strategy snapshot has no execution specification")


def _account_daily(
    prices: pd.DataFrame,
    target: pd.Series,
    initial_target: float,
    initial_signal_date: pd.Timestamp,
    equity: pd.Series,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(pd.to_datetime(prices["dt"]), name="date")
    desired = target.reindex(index).astype(float)
    execution_target = desired.shift(1)
    execution_target.iloc[0] = float(initial_target)
    signal_dates = pd.Series(index=index, dtype="datetime64[ns]")
    signal_dates.iloc[0] = initial_signal_date
    signal_dates.iloc[1:] = index[:-1].to_numpy()
    return pd.DataFrame(
        {
            "date": index,
            "signal_date": signal_dates.to_numpy(),
            "target_position": execution_target.to_numpy(),
            "close": prices["close"].astype(float).to_numpy(),
            "equity": equity.reindex(index).astype(float).to_numpy(),
        }
    )


def replay_benchmarks(
    signals: SignalReplay,
    replay_data: ReplayData,
    initial_cash: float,
) -> BenchmarkReplay:
    """Run independently funded BuyHold and MA5/MA20 next-open benchmarks."""
    fee_rate = _fee_rate(signals)
    execution = replay_data.execution_daily.copy()
    execution["dt"] = pd.to_datetime(execution["dt"]).dt.normalize()
    evaluation = execution.loc[
        execution["dt"].between(signals.evaluation_start, signals.evaluation_end)
    ].copy()
    evaluation_index = pd.DatetimeIndex(evaluation["dt"], name="dt")
    if evaluation.empty:
        raise ValueError("benchmark interval contains no execution sessions")

    adjusted_signals = moving_average_signals(replay_data.adjusted.daily)
    prior_dates = adjusted_signals.index[adjusted_signals.index < signals.evaluation_start]
    if prior_dates.empty:
        raise ValueError("benchmark interval has no prior signal session")
    prior_date = pd.Timestamp(prior_dates[-1])

    buyhold_target = pd.Series(1.0, index=evaluation_index, name="target_position")
    buyhold = run_next_open_backtest(
        evaluation,
        buyhold_target,
        fee_rate=fee_rate,
        init_cash=initial_cash,
        initial_target=1.0,
        initial_signal_date=prior_date,
    )
    buyhold_metrics = strategy_comparison_metrics(
        buyhold.equity, buyhold.orders, initial_cash
    )
    buyhold_metrics["closed_trades"] = 0
    buyhold_account = _account_daily(
        evaluation,
        buyhold_target,
        1.0,
        prior_date,
        buyhold.equity,
    )

    ma_target = adjusted_signals["target_position"].reindex(evaluation_index).astype(float)
    initial_ma_target = float(adjusted_signals.loc[prior_date, "target_position"])
    ma = run_next_open_backtest(
        evaluation,
        ma_target,
        fee_rate=fee_rate,
        init_cash=initial_cash,
        initial_target=initial_ma_target,
        initial_signal_date=prior_date,
    )
    ma_trades = closed_trade_ledger(ma.orders)
    ma_metrics = strategy_comparison_metrics(ma.equity, ma.orders, initial_cash)
    ma_metrics["closed_trades"] = int(len(ma_trades))
    ma_account = _account_daily(
        evaluation,
        ma_target,
        initial_ma_target,
        prior_date,
        ma.equity,
    )
    visible_signals = adjusted_signals.loc[prior_date : signals.evaluation_end].reset_index()
    visible_signals = visible_signals.rename(columns={"dt": "date"})
    return BenchmarkReplay(
        metrics={
            "buyhold": {"metrics": buyhold_metrics},
            "ma5_ma20": {"metrics": ma_metrics},
        },
        buyhold_account_daily=buyhold_account,
        ma_signals=visible_signals,
        ma_orders=ma.orders,
        ma_account_daily=ma_account,
        ma_trades=ma_trades,
    )
