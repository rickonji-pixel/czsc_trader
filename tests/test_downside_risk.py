from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from czsc_trader.audit import audit_no_lookahead
from czsc_trader.backtest import run_period_backtests


def test_downside_volatility_uses_only_negative_log_returns() -> None:
    from czsc_trader.downside_risk import downside_volatility

    index = pd.date_range("2025-01-02", periods=6, freq="B", name="dt")
    close = pd.Series([100.0, 110.0, 99.0, 99.0, 89.1, 98.01], index=index)
    log_returns = np.log(np.array([99.0, 99.0, 89.1, 98.01]) / np.array([110.0, 99.0, 99.0, 89.1]))
    expected = np.sqrt(252.0 * np.mean(np.minimum(log_returns, 0.0) ** 2))

    result = downside_volatility(close, lookback=4)

    assert result.iloc[-1] == pytest.approx(expected)


def test_stress_threshold_excludes_the_current_risk_value() -> None:
    from czsc_trader.downside_risk import (
        downside_stress,
        downside_stress_threshold,
    )

    index = pd.date_range("2024-01-02", periods=253, freq="B", name="dt")
    risk = pd.Series([1.0] * 252 + [100.0], index=index)

    threshold = downside_stress_threshold(risk, quantile=0.8, history=252)
    stress = downside_stress(risk, quantile=0.8, history=252)
    mutated = risk.copy()
    mutated.iloc[-1] = 200.0
    mutated_threshold = downside_stress_threshold(
        mutated, quantile=0.8, history=252
    )

    assert threshold.iloc[-1] == pytest.approx(1.0)
    assert mutated_threshold.iloc[-1] == pytest.approx(1.0)
    assert bool(stress.iloc[-1]) is True
    assert not stress.iloc[:-1].any()


def test_downside_risk_target_never_overrides_a_flat_champion() -> None:
    from czsc_trader.downside_risk import downside_risk_target

    index = pd.date_range("2025-01-02", periods=5, freq="B", name="dt")
    baseline = pd.Series([0.0, 1.0, 1.0, 1.0, 0.0], index=index)
    stress = pd.Series([False, False, True, False, True], index=index)

    target = downside_risk_target(baseline, stress, pressure_position=0.5)

    assert target.tolist() == [0.0, 1.0, 0.5, 1.0, 0.0]


def test_downside_risk_spec_has_stable_candidate_identity() -> None:
    from czsc_trader.downside_risk import DownsideRiskSpec

    spec = DownsideRiskSpec(20, 0.8, 0.5)

    assert spec.candidate_id == "dv_L20_Q80_P0.50"


def test_downside_risk_events_drive_exact_next_open_resize_orders() -> None:
    from czsc_trader.downside_risk import (
        DownsideRiskSpec,
        build_downside_risk_events,
    )

    index = pd.date_range("2026-01-05", periods=7, freq="B", name="dt")
    daily = pd.DataFrame({"open": 100.0, "close": 100.0}, index=index)
    baseline = pd.Series([0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0], index=index)
    target = pd.Series([0.0, 0.5, 1.0, 0.5, 0.0, 0.0, 0.0], index=index)
    scores = pd.Series([0.0, 0.2, 0.2, 0.2, 0.0, 0.0, 0.0], index=index)
    risk = pd.Series([0.1, 0.4, 0.2, 0.5, 0.2, 0.2, 0.2], index=index)
    threshold = pd.Series([0.3] * len(index), index=index)
    stress = risk.gt(threshold)
    spec = DownsideRiskSpec(20, 0.8, 0.5)

    events = build_downside_risk_events(
        target,
        baseline,
        scores,
        risk,
        threshold,
        stress,
        spec,
    )
    factor_frame = pd.DataFrame({"factor_score": scores}, index=index)
    result = run_period_backtests(
        daily,
        target,
        {"P": (index[1], index[-1])},
        factor_events=events,
        factor_frame=factor_frame,
    )["P"]

    assert events["event_type"].tolist() == [
        "Entry",
        "Increase",
        "Reduce",
        "Exit",
    ]
    assert events["before_position"].tolist() == [0.0, 0.5, 1.0, 0.5]
    assert events["after_position"].tolist() == [0.5, 1.0, 0.5, 0.0]
    assert events["candidate_id"].unique().tolist() == [spec.candidate_id]
    assert result.orders["side"].tolist() == ["Buy", "Buy", "Sell", "Sell"]
    assert result.orders["event_type"].tolist() == [
        "Entry",
        "Increase",
        "Reduce",
        "Exit",
    ]
    assert audit_no_lookahead(
        result.orders,
        result.factor_events,
        target,
        factor_frame,
    )["status"] == "PASS"
