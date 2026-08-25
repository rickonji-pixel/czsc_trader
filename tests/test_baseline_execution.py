from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import ResolvedBaseline
from czsc_trader.four_layer import (
    normalized_signal_factors,
    positions_from_scores,
    score_four_layer,
)
from czsc_trader.rules import Rule, apply_fixed_rule


FACTOR_NAMES = (
    "raw__30m__cxt_bi_status_V230101",
    "raw__30m__cxt_third_buy_V230228__di_1",
    "raw__daily__cxt_bi_status_V230101",
    "raw__daily__cxt_five_bi_V230619__di_1",
    "raw__daily__cxt_seven_bi_V230620__di_1",
    "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5",
    "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10",
    "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20",
    "raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26",
    "raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5",
    "raw__daily__pressure_support_V240406__di_1__w_20",
    "raw__weekly__cxt_bi_status_V230101",
)
FACTOR_WEIGHTS = (0.05,) * 10 + (0.25, 0.25)


def _four_layer_baseline(
    *, factor_names: tuple[str, ...] = FACTOR_NAMES
) -> ResolvedBaseline:
    rule = Rule((0.3, 0.3, 0.4), 0.175, 0.025, 1, 3, 1, "none")
    return ResolvedBaseline(
        version="baseline_20260826",
        rule=rule,
        rule_payload={},
        sha256="a" * 64,
        verification_snapshot="",
        strategy="czsc_four_layer",
        status="active",
        scope="symbol",
        symbol="588080.SH",
        factor_names=factor_names,
        factor_weights=FACTOR_WEIGHTS,
    )


def _raw_frame() -> pd.DataFrame:
    index = pd.date_range("2026-01-05", periods=4, freq="D", name="dt")
    frame = pd.DataFrame(index=index)
    for name in FACTOR_NAMES:
        frame[name] = ["多头", "空头", "空头", "空头"]
    frame["structure"] = [1.0, -1.0, -1.0, -1.0]
    frame["trend"] = [1.0, -1.0, -1.0, -1.0]
    frame["volume_position"] = [1.0, -1.0, -1.0, -1.0]
    return frame


def test_four_layer_adapter_matches_direct_frozen_execution() -> None:
    baseline = _four_layer_baseline()
    frame = _raw_frame()
    mapped = normalized_signal_factors(frame.loc[:, list(FACTOR_NAMES)])
    weights = pd.Series(FACTOR_WEIGHTS, index=FACTOR_NAMES, dtype=float)
    expected_scores = score_four_layer(mapped, weights)
    expected_target = positions_from_scores(
        expected_scores, baseline.rule.enter, baseline.rule.exit, baseline.rule
    )

    applied = apply_resolved_baseline(frame, baseline)

    pd.testing.assert_series_equal(applied.scores, expected_scores)
    pd.testing.assert_series_equal(applied.target_position, expected_target)
    assert applied.target_position.to_list() == [1.0, 1.0, 1.0, 0.0]
    assert applied.events["event_id"].to_list() == [
        "FourLayer:20260105:Entry",
        "FourLayer:20260108:Exit",
    ]
    assert applied.events["factor_score"].to_list() == pytest.approx([1.0, -1.0])


def test_four_layer_adapter_rejects_missing_or_duplicate_frozen_factors() -> None:
    missing = _raw_frame().drop(columns=[FACTOR_NAMES[-1]])
    with pytest.raises(ValueError, match="missing frozen factors"):
        apply_resolved_baseline(missing, _four_layer_baseline())

    duplicated = FACTOR_NAMES[:-1] + (FACTOR_NAMES[0],)
    with pytest.raises(ValueError, match="unique"):
        apply_resolved_baseline(_raw_frame(), _four_layer_baseline(factor_names=duplicated))


def test_legacy_adapter_is_identical_to_existing_fixed_rule_execution() -> None:
    frame = _raw_frame()
    rule = Rule((0.3, 0.3, 0.4), 0.15, 0.0, 1, 3, 1, "none")
    baseline = ResolvedBaseline(
        version="baseline_20260823",
        rule=rule,
        rule_payload={},
        sha256="b" * 64,
        verification_snapshot="",
        strategy="czsc_fixed_rule",
        status="archived",
        scope="historical_generic",
    )

    expected = apply_fixed_rule(frame, rule)
    applied = apply_resolved_baseline(frame, baseline)

    pd.testing.assert_series_equal(applied.scores, expected.scores)
    pd.testing.assert_series_equal(applied.target_position, expected.target_position)
    pd.testing.assert_frame_equal(applied.events, expected.events)
