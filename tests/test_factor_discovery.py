from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from czsc_trader.factor_discovery import (
    build_signal_support,
    build_candidate_factors,
    independent_event_count,
    is_event_primary,
    predeclared_interactions,
    rank_factor_results,
    sparse_coordinate_optimize,
    validate_sparse_weights,
)


def _protocol(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "state_min_coverage": 0.8,
        "state_min_active_days": 2,
        "state_max_active_ratio": 0.8,
        "maximum_active_factors": 4,
        "minimum_absolute_weight": 0.1,
        "minimum_trend_weight": 0.0,
        "protect_volume_window": False,
    }
    payload.update(overrides)
    return payload


def test_event_primary_and_independent_occurrences() -> None:
    indicator = pd.Series([0, 1, 1, 0, 1, 0, 0, 1], dtype=float)

    assert is_event_primary("一买")
    assert is_event_primary("类三卖")
    assert is_event_primary("aAb式底背驰")
    assert is_event_primary("类趋势顶背驰")
    assert not is_event_primary("向上")
    assert not is_event_primary("其他")
    assert independent_event_count(indicator) == 3


def test_sparse_event_is_retained_but_sparse_state_is_filtered() -> None:
    index = pd.date_range("2021-01-01", periods=40, freq="D")
    raw = pd.DataFrame(
        {
            "raw__daily__event": ["其他_x"] * 39 + ["一买_x"],
            "raw__daily__state": ["其他_x"] * 39 + ["向上_x"],
        },
        index=index,
    )
    base = pd.DataFrame({"base": np.ones(40)}, index=index)

    result = build_candidate_factors(
        raw,
        base,
        pd.Series({"base": 1.0}),
        _protocol(state_min_active_days=30, state_max_active_ratio=0.9),
    )

    assert "state__raw__daily__event::一买" in result.factors
    assert "state__raw__daily__state::向上" not in result.factors
    event = next(row for row in result.metadata["states"] if row["primary"] == "一买")
    assert event["factor_kind"] == "event"
    assert event["independent_events"] == 1


def test_duplicate_events_keep_canonical_factor_and_alias_metadata() -> None:
    index = pd.date_range("2021-01-01", periods=4, freq="D")
    raw = pd.DataFrame(
        {
            "raw__daily__a": ["其他_x", "一买_x", "其他_x", "其他_x"],
            "raw__daily__b": ["其他_x", "三买_x", "其他_x", "其他_x"],
        },
        index=index,
    )
    base = pd.DataFrame({"base": np.ones(4)}, index=index)

    result = build_candidate_factors(
        raw,
        base,
        pd.Series({"base": 1.0}),
        _protocol(state_min_active_days=1, state_max_active_ratio=1.0),
    )

    events = [row for row in result.metadata["states"] if row["factor_kind"] == "event"]
    assert [row["status"] for row in events] == ["candidate", "aliased_duplicate"]
    assert events[1]["canonical_factor"] == events[0]["factor"]
    assert events[0]["factor"] in result.factors
    assert events[1]["factor"] not in result.factors


def test_signal_support_distinguishes_unavailable_generated_and_observed() -> None:
    support = build_signal_support(
        available_names={"cxt_first_buy_V221126", "cxt_first_sell_V221126"},
        requirements=[
            "cxt_first_buy_V221126",
            "cxt_first_sell_V221126",
            "cxt_second_buy_V230320",
        ],
        raw_names=[
            "raw__daily__cxt_first_buy_V221126__di_1",
            "raw__daily__cxt_first_sell_V221126__di_1",
        ],
        state_records=[
            {
                "raw_signal": "raw__daily__cxt_first_buy_V221126__di_1",
                "factor_kind": "event",
                "status": "candidate",
            }
        ],
    )

    assert support["cxt_first_buy_V221126"]["status"] == "observed"
    assert support["cxt_first_sell_V221126"]["status"] == "generated"
    assert support["cxt_second_buy_V230320"]["status"] == "unavailable_in_czsc_1_0_1"


def test_state_expansion_filters_and_deduplicates_deterministically() -> None:
    index = pd.date_range("2025-01-01", periods=10, freq="D")
    raw = pd.DataFrame(
        {
            "raw__daily__signal_a": ["多头_x", "多头_x", "多头_x", "多头_x", "空头_x", "空头_x", "空头_x", "空头_x", "其他_x", "其他_x"],
            "raw__daily__signal_b": ["多头_y", "多头_y", "多头_y", "多头_y", "空头_y", "空头_y", "空头_y", "空头_y", "其他_y", "其他_y"],
            "raw__daily__sparse": [None] * 8 + ["事件_x", "事件_x"],
        },
        index=index,
    )
    base = pd.DataFrame({"raw__daily__base": np.linspace(-1, 1, 10)}, index=index)
    result = build_candidate_factors(raw, base, pd.Series({"raw__daily__base": 1.0}), _protocol())

    assert list(result.factors.columns) == [
        "raw__daily__base",
        "state__raw__daily__signal_a::其他",
        "state__raw__daily__signal_a::多头",
        "state__raw__daily__signal_a::空头",
    ]
    assert not any("signal_b" in name for name in result.factors)
    assert not any("sparse" in name for name in result.factors)
    assert result.metadata["deduplicated_state_count"] == 3


def test_frozen_factor_names_replay_unseen_state_as_zero() -> None:
    index = pd.date_range("2026-01-01", periods=3, freq="D")
    raw = pd.DataFrame({"raw__daily__signal": ["多头_x", "未知_x", "多头_x"]}, index=index)
    base = pd.DataFrame({"raw__daily__base": [1.0, 0.0, 1.0]}, index=index)
    names = ["raw__daily__base", "state__raw__daily__signal::空头"]

    result = build_candidate_factors(
        raw,
        base,
        pd.Series({"raw__daily__base": 1.0}),
        _protocol(),
        frozen_names=names,
    )

    assert list(result.factors.columns) == names
    assert result.factors.iloc[:, 1].eq(0.0).all()


def test_predeclared_interactions_capture_bull_and_bear_consensus() -> None:
    index = pd.date_range("2025-01-01", periods=3, freq="D")
    columns = {
        "raw__30m__cxt_bi_status_V230101": [1, -1, 1],
        "raw__daily__cxt_bi_status_V230101": [1, -1, -1],
        "raw__weekly__cxt_bi_status_V230101": [1, -1, 1],
        "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5": [1, -1, 1],
        "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10": [1, -1, 1],
        "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20": [1, -1, -1],
        "raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26": [1, -1, 1],
    }
    interactions = predeclared_interactions(pd.DataFrame(columns, index=index))

    assert list(interactions.columns) == [
        "interaction__bi_all_bull",
        "interaction__bi_all_bear",
        "interaction__trend_all_bull",
        "interaction__trend_all_bear",
    ]
    assert interactions.iloc[0].tolist() == [1.0, 0.0, 1.0, 0.0]
    assert interactions.iloc[1].tolist() == [0.0, 1.0, 0.0, 1.0]
    assert interactions.iloc[2].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_sparse_weight_validation_enforces_cap_and_protection() -> None:
    weights = pd.Series(
        {
            "raw__daily__tas_ma_base_V221101__timeperiod_5": 0.4,
            "raw__daily__vol_window_V230731": 0.2,
            "state__x::bull": 0.4,
        }
    )
    protocol = _protocol(
        maximum_active_factors=3,
        minimum_absolute_weight=0.1,
        minimum_trend_weight=0.3,
        protect_volume_window=True,
    )
    validate_sparse_weights(weights, list(weights.index), protocol)

    weak_trend = pd.Series(
        {
            "raw__daily__tas_ma_base_V221101__timeperiod_5": 0.05,
            "raw__daily__vol_window_V230731": 0.25,
            "state__x::bull": 0.70,
        }
    )
    with pytest.raises(ValueError, match="trend"):
        validate_sparse_weights(weak_trend, list(weak_trend.index), protocol)

    no_volume = pd.Series(
        {
            "raw__daily__tas_ma_base_V221101__timeperiod_5": 0.5,
            "state__x::bull": 0.5,
        }
    )
    with pytest.raises(ValueError, match="volume"):
        validate_sparse_weights(no_volume, list(no_volume.index), protocol)


def test_sparse_optimizer_can_activate_zero_weight_and_is_deterministic() -> None:
    origin = pd.Series({"base": 1.0, "new": 0.0})
    protocol = _protocol(maximum_active_factors=2, minimum_absolute_weight=0.1)

    def objective(weights: pd.Series) -> tuple[float, ...]:
        return (float(weights["new"]), -float(weights["base"]))

    first = sparse_coordinate_optimize(origin, 0.2, 2, protocol, objective)
    second = sparse_coordinate_optimize(origin, 0.2, 2, protocol, objective)

    assert first["new"] > 0.0
    assert first.abs().sum() == pytest.approx(1.0)
    pd.testing.assert_series_equal(first, second)


def test_return_ranking_ignores_sharpe_and_prefers_sparse_tie() -> None:
    rows = pd.DataFrame(
        [
            {"spec_id": "dense", "win_count": 6, "min_return_delta": -0.01, "median_return_delta": 0.02, "mean_return_delta": 0.03, "mean_active_factors": 17, "weight_shift": 0.1, "sharpe": 99.0},
            {"spec_id": "sparse", "win_count": 6, "min_return_delta": -0.01, "median_return_delta": 0.02, "mean_return_delta": 0.03, "mean_active_factors": 14, "weight_shift": 0.1, "sharpe": -99.0},
        ]
    )

    ranked = rank_factor_results(rows)

    assert ranked.iloc[0]["spec_id"] == "sparse"
