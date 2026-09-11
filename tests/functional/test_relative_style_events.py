from __future__ import annotations

import numpy as np
import pandas as pd

from czsc_trader.relative_style_events import (
    MECHANISMS,
    build_relative_style_features,
    generate_price_volume_confirmation_events,
    generate_relative_style_events,
)


def test_relative_style_events_are_causal_and_return_free() -> None:
    dates = pd.bdate_range("2025-01-02", periods=180)
    rows = []
    for symbol, slope in (("510300.SH", 0.0005), ("510500.SH", 0.001), ("512100.SH", 0.0002)):
        close = 2.0 * np.exp(slope * np.arange(len(dates)) + 0.01 * np.sin(np.arange(len(dates))))
        for index, dt in enumerate(dates):
            rows.append(
                {
                    "dt": dt,
                    "symbol": symbol,
                    "open": close[index] * (1 + 0.002 * np.cos(index)),
                    "high": close[index] * 1.01,
                    "low": close[index] * 0.99,
                    "close": close[index],
                    "vol": 1000 + index,
                    "amount": (1000 + index) * close[index],
                    "observed": True,
                    "source_dt": dt,
                    "staleness_sessions": 0,
                }
            )
    panel = pd.DataFrame(rows)
    features = build_relative_style_features(panel)
    events, density, annual, overlap = generate_relative_style_events(
        features, evaluation_start=dates[80]
    )

    assert not any("forward" in column or "return" in column for column in features.columns)
    assert set(density["mechanism"]) == set(MECHANISMS)
    assert set(overlap.index) == set(MECHANISMS)
    assert set(annual["mechanism"]) == set(MECHANISMS)
    assert (events.loc[events["execution_clock"].eq("NEXT_OPEN"), "event_date"] > events.loc[
        events["execution_clock"].eq("NEXT_OPEN"), "signal_date"
    ]).all()


def test_price_volume_confirmation_is_fixed_before_next_session() -> None:
    dates = pd.bdate_range("2025-01-02", periods=260)
    features = pd.DataFrame(
        {
            "dt": dates,
            "relative_strength_5": np.sin(np.arange(len(dates)) / 5) * 0.02 + 0.01,
            "relative_amount_impulse": np.cos(np.arange(len(dates)) / 7),
            "clean_20_session_window": True,
        }
    )
    events, density, evidence = generate_price_volume_confirmation_events(
        features, evaluation_start=dates[140]
    )

    assert (events["event_date"] > events["signal_date"]).all()
    assert set(density["mechanism"]) == {"PRICE_VOLUME_ROTATION_CONFIRMATION"}
    assert not any("forward" in column for column in evidence.columns)
