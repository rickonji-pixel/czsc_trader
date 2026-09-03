import pandas as pd

from czsc_trader.audit import audit_candidate_evaluation
from czsc_trader.backtest import PeriodBacktestResult


def test_candidate_evaluation_audit_checks_all_windows_without_provenance_frames():
    dates = pd.date_range("2026-01-02", periods=5, freq="B")
    target = pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=dates)
    scores = pd.Series([0.0, 0.2, 0.1, -0.1, 0.0], index=dates)
    events = pd.DataFrame([
        {
            "event_id": "entry",
            "signal_date": dates[1],
            "event_type": "Entry",
            "factor_score": 0.2,
            "before_position": 0.0,
            "after_position": 1.0,
        },
        {
            "event_id": "exit",
            "signal_date": dates[3],
            "event_type": "Exit",
            "factor_score": -0.1,
            "before_position": 1.0,
            "after_position": 0.0,
        },
    ])
    orders = pd.DataFrame([
        {
            "signal_date": dates[1],
            "execution_date": dates[2],
            "side": "Buy",
            "size": 10.0,
            "price": 10.0,
            "fees": 0.05,
        },
        {
            "signal_date": dates[3],
            "execution_date": dates[4],
            "side": "Sell",
            "size": 10.0,
            "price": 11.0,
            "fees": 0.05,
        },
    ])
    result = PeriodBacktestResult(
        None,
        pd.Series([100.0, 110.0], index=dates[2:4]),
        orders,
        {"start": str(dates[1].date())},
        pd.DataFrame(),
    )
    audit = audit_candidate_evaluation({"full": result}, target, events, scores)
    assert audit == {
        "status": "PASS",
        "windows_checked": 1,
        "orders_checked": 2,
        "positions_checked": 5,
    }
