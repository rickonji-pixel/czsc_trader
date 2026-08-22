import pandas as pd
import pytest

from czsc_trader.audit import audit_no_lookahead


def _valid_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    orders = pd.DataFrame(
        {
            "signal_date": pd.to_datetime(["2025-01-02", "2025-01-06"]),
            "execution_date": pd.to_datetime(["2025-01-03", "2025-01-07"]),
        }
    )
    selections = pd.DataFrame(
        {
            "as_of_date": pd.to_datetime(["2025-01-02", "2025-02-03"]),
            "train_start": pd.to_datetime(["2024-01-02", "2024-02-01"]),
            "train_end": pd.to_datetime(["2024-12-31", "2025-01-31"]),
            "candidate_count": [126, 126],
        }
    )
    target = pd.Series([0.0, 1.0, 0.0], index=pd.bdate_range("2025-01-02", periods=3))
    return orders, selections, target


def test_audit_accepts_strictly_causal_records() -> None:
    """Catch false audit failures on valid temporal records."""
    orders, selections, target = _valid_inputs()
    result = audit_no_lookahead(orders, selections, target)
    assert result["status"] == "PASS"


def test_audit_rejects_same_day_execution() -> None:
    """Catch execution at the same timestamp that generated the signal."""
    orders, selections, target = _valid_inputs()
    orders.loc[0, "execution_date"] = orders.loc[0, "signal_date"]

    with pytest.raises(AssertionError, match="execution_date"):
        audit_no_lookahead(orders, selections, target)


def test_audit_rejects_training_through_parameter_date() -> None:
    """Catch monthly tuning that includes its own effective date."""
    orders, selections, target = _valid_inputs()
    selections.loc[0, "train_end"] = selections.loc[0, "as_of_date"]

    with pytest.raises(AssertionError, match="train_end"):
        audit_no_lookahead(orders, selections, target)

