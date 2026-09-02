from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from czsc_trader.strategy_metrics import annualized_sharpe


def test_annualized_sharpe_includes_first_session_return() -> None:
    equity = pd.Series([102.0, 101.0, 104.0])
    returns = pd.Series([0.02, 101.0 / 102.0 - 1.0, 104.0 / 101.0 - 1.0])
    expected = np.sqrt(252.0) * returns.mean() / returns.std(ddof=1)

    assert annualized_sharpe(equity, 100.0) == pytest.approx(expected)


def test_annualized_sharpe_rejects_degenerate_series() -> None:
    assert annualized_sharpe(pd.Series([100.0]), 100.0) is None
    assert annualized_sharpe(pd.Series([100.0, 100.0]), 100.0) is None


def test_annualized_sharpe_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="equity must be finite and non-empty"):
        annualized_sharpe(pd.Series([100.0, np.nan]), 100.0)
    with pytest.raises(ValueError, match="initial cash must be positive and finite"):
        annualized_sharpe(pd.Series([100.0, 101.0]), 0.0)
