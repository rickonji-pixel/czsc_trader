from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from czsc_trader.audit import audit_no_lookahead
from czsc_trader.backtest import run_period_backtests


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_downside_risk_protocol_builds_exact_preregistered_grid() -> None:
    from czsc_trader.downside_risk_runner import (
        build_downside_risk_specs,
        validate_downside_risk_protocol,
    )

    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0830_EX01"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )

    validate_downside_risk_protocol(protocol)
    specs = build_downside_risk_specs(protocol)

    assert len(specs) == 27
    assert len({spec.candidate_id for spec in specs}) == 27
    assert specs[0].candidate_id == "dv_L10_Q70_P0.25"
    assert specs[-1].candidate_id == "dv_L40_Q90_P0.75"

    changed = {**protocol, "stress_history": 251}
    with pytest.raises(ValueError, match="stress_history"):
        validate_downside_risk_protocol(changed)


def test_candidate_ranking_excludes_ineligible_rows_before_drawdown_sort() -> None:
    from czsc_trader.downside_risk_runner import rank_eligible_candidates

    rows = pd.DataFrame(
        [
            {
                "candidate_id": "eligible_a",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.20,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.17,
                "max_drawdown_improvement": 0.03,
                "worst_annual_return_delta": -0.03,
                "full_trade_count": 20,
            },
            {
                "candidate_id": "eligible_b",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.21,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.17,
                "max_drawdown_improvement": 0.03,
                "worst_annual_return_delta": -0.02,
                "full_trade_count": 25,
            },
            {
                "candidate_id": "eligible_c",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.22,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.17,
                "max_drawdown_improvement": 0.03,
                "worst_annual_return_delta": -0.02,
                "full_trade_count": 15,
            },
            {
                "candidate_id": "lower_return",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.19,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.10,
                "max_drawdown_improvement": 0.10,
                "worst_annual_return_delta": 0.10,
                "full_trade_count": 1,
            },
            {
                "candidate_id": "flat_drawdown",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.30,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.20,
                "max_drawdown_improvement": 0.0,
                "worst_annual_return_delta": 0.10,
                "full_trade_count": 1,
            },
        ]
    )

    ranked = rank_eligible_candidates(rows)

    assert ranked["candidate_id"].tolist() == [
        "eligible_c",
        "eligible_b",
        "eligible_a",
    ]


def test_2026_objective_accepts_equal_return_but_requires_better_drawdown() -> None:
    from czsc_trader.downside_risk_runner import downside_risk_2026_pass

    champion = {"strategy_return": 0.50, "max_drawdown": -0.20}

    assert downside_risk_2026_pass(
        champion,
        {"strategy_return": 0.50, "max_drawdown": -0.19},
    )
    assert not downside_risk_2026_pass(
        champion,
        {"strategy_return": 0.50, "max_drawdown": -0.20},
    )
    assert not downside_risk_2026_pass(
        champion,
        {"strategy_return": 0.49, "max_drawdown": -0.10},
    )
