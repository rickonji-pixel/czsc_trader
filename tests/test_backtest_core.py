import numpy as np
import pandas as pd

from czsc_trader.backtest import _independent_equity


def _reference_equity(prices, execution_target, fee_rate, init_cash):
    cash = float(init_cash)
    shares = 0.0
    previous_target = 0.0
    values = []
    for date, row in prices.iterrows():
        desired = float(execution_target.loc[date])
        open_price = float(row["open"])
        if desired != previous_target:
            portfolio_value = cash + shares * open_price
            current_asset_value = shares * open_price
            asset_value_delta = desired * portfolio_value - current_asset_value
            if asset_value_delta > 0.0:
                requested_shares = asset_value_delta / open_price
                affordable_shares = cash / (open_price * (1.0 + fee_rate))
                bought = min(requested_shares, affordable_shares)
                cash -= bought * open_price * (1.0 + fee_rate)
                shares += bought
            else:
                sold = min(-asset_value_delta / open_price, shares)
                cash += sold * open_price * (1.0 - fee_rate)
                shares -= sold
            previous_target = desired
        values.append(cash + shares * float(row["close"]))
    return np.asarray(values)


def test_array_independent_equity_matches_reference_state_machine():
    dates = pd.date_range("2026-01-05", periods=7, freq="B")
    prices = pd.DataFrame({
        "open": [10.0, 11.0, 12.0, 11.5, 11.0, 12.0, 12.5],
        "close": [10.5, 12.0, 11.0, 11.0, 12.0, 12.5, 12.0],
    }, index=dates)
    target = pd.Series([0.0, 0.5, 1.0, 1.0, 0.25, 0.0, 0.0], index=dates)
    actual = _independent_equity(prices, target, 0.0005, 1_000_000.0)
    expected = _reference_equity(prices, target, 0.0005, 1_000_000.0)
    np.testing.assert_array_equal(actual.to_numpy(), expected)
    assert actual.index.equals(dates)
