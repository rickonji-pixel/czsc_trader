from collections import Counter
from pathlib import Path

import pandas as pd

import czsc_trader.factors as factors
from czsc_trader.data import load_market_data
from czsc_trader.factors import (
    aggregate_signal_groups,
    generate_factor_frame,
    map_signal_frame,
    signal_groups,
    signal_primary,
)


RAW_DIR = Path("data/raw")
GROUPS = ["structure", "trend", "volume_position"]


def test_divergence_signals_are_structure_inputs() -> None:
    """Catch missing CZSC divergence inputs or reversed bullish/bearish mapping."""
    names = {item["name"] for item in factors.DAILY_CONFIG}
    assert {"cxt_five_bi_V230619", "cxt_seven_bi_V230620"} <= names
    unknown = Counter()
    assert factors._signal_score("底背驰_任意_任意_0", unknown) == 1.0
    assert factors._signal_score("顶背驰_任意_任意_0", unknown) == -1.0


def test_factors_are_daily_grouped_and_bounded() -> None:
    """Catch intraday leakage, missing CZSC outputs, or unscaled groups."""
    data = load_market_data(RAW_DIR)
    result = generate_factor_frame(data)

    assert result.frame.index.equals(pd.DatetimeIndex(data.daily["dt"], name="dt"))
    assert result.frame.index.is_unique
    assert result.frame.index.is_monotonic_increasing
    assert set(GROUPS).issubset(result.frame.columns)
    assert result.frame[GROUPS].abs().le(1).all().all()
    assert len([column for column in result.frame if column.startswith("raw__")]) >= 8


def test_public_signal_internals_rebuild_exact_group_scores() -> None:
    """Catch attribution helpers that diverge from live factor generation."""
    data = load_market_data(RAW_DIR, cutoff="2021-03-26")
    result = generate_factor_frame(data)
    raw = result.frame.filter(like="raw__")

    mapped, unknown = map_signal_frame(raw)
    groups = signal_groups(mapped.columns)
    rebuilt = aggregate_signal_groups(mapped, groups)

    pd.testing.assert_frame_equal(rebuilt[GROUPS], result.frame[GROUPS])
    assert signal_primary("多头_任意_任意_0") == "多头"
    assert signal_primary(None) is None
    assert unknown == result.unknown_values
    assert set(groups) == set(GROUPS)


def test_future_truncation_does_not_change_past_factors() -> None:
    """Catch batch signal generation that can revise earlier dates."""
    data = load_market_data(RAW_DIR)
    cutoff = pd.Timestamp("2025-06-30")

    full = generate_factor_frame(data).frame.loc[:cutoff]
    truncated = generate_factor_frame(data.truncate(cutoff)).frame

    pd.testing.assert_frame_equal(full, truncated)
