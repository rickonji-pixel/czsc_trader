import pandas as pd
import pytest

from czsc_trader.audit import audit_no_lookahead


def _valid_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.DataFrame]:
    dates = pd.bdate_range("2025-01-02", periods=3)
    orders = pd.DataFrame(
        {
            "period": ["sample"],
            "signal_date": [dates[0]],
            "execution_date": [dates[1]],
            "side": ["Buy"],
            "factor_event_id": ["Factor:20250102:Entry"],
            "event_type": ["Entry"],
        }
    )
    events = pd.DataFrame(
        {
            "event_id": ["Factor:20250102:Entry"],
            "signal_date": [dates[0]],
            "event_type": ["Entry"],
            "factor_score": [0.4],
            "after_position": [1.0],
        }
    )
    target = pd.Series([1.0, 1.0, 0.0], index=dates, name="target_position")
    factor_frame = pd.DataFrame({"factor_score": [0.4, 0.3, -0.4]}, index=dates)
    return orders, events, target, factor_frame


def test_audit_accepts_strictly_causal_factor_provenance() -> None:
    """Catch false audit failures on a matched next-open factor order."""
    orders, events, target, factors = _valid_inputs()
    result = audit_no_lookahead(orders, events, target, factors)
    assert result["status"] == "PASS"
    assert result["events_checked"] == 1


def test_audit_rejects_same_day_execution() -> None:
    """Catch execution at the same timestamp that generated the signal."""
    orders, events, target, factors = _valid_inputs()
    orders.loc[0, "execution_date"] = orders.loc[0, "signal_date"]

    with pytest.raises(AssertionError, match="execution_date"):
        audit_no_lookahead(orders, events, target, factors)


def test_audit_rejects_execution_after_next_trading_day() -> None:
    """Catch a signal being delayed beyond the mandated next session."""
    orders, events, target, factors = _valid_inputs()
    orders.loc[0, "execution_date"] = target.index[2]

    with pytest.raises(AssertionError, match="next trading date"):
        audit_no_lookahead(orders, events, target, factors)


def test_audit_rejects_order_without_factor_event() -> None:
    """Catch any trade introduced outside the CZSC factor state machine."""
    orders, events, target, factors = _valid_inputs()
    orders.loc[0, "factor_event_id"] = "Missing"

    with pytest.raises(AssertionError, match="factor event"):
        audit_no_lookahead(orders, events, target, factors)


def test_audit_rejects_direction_mismatch() -> None:
    """Catch a buy order falsely attributed to an exit event."""
    orders, events, target, factors = _valid_inputs()
    orders.loc[0, "event_type"] = "Exit"

    with pytest.raises(AssertionError, match="direction"):
        audit_no_lookahead(orders, events, target, factors)

