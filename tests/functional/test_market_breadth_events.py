from __future__ import annotations

import numpy as np
import pandas as pd

from czsc_trader.market_breadth_events import (
    MECHANISM,
    build_market_breadth_features,
    generate_breadth_thrust_events,
)


def test_breadth_events_are_point_in_time_and_return_free() -> None:
    dates = pd.bdate_range("2025-01-02", periods=180)
    rows = []
    for day_index, dt in enumerate(dates):
        for member in range(20):
            rows.append(
                {
                    "dt": dt,
                    "con_code": f"{member:06d}.SZ",
                    "record_status": "OBSERVED",
                    "pct_chg": np.sin(day_index / 5 + member / 3),
                }
            )
    features = build_market_breadth_features(pd.DataFrame(rows))
    events, density, evidence = generate_breadth_thrust_events(
        features, evaluation_start=dates[100]
    )

    assert (events["event_date"] > events["signal_date"]).all()
    assert set(density["mechanism"]) == {MECHANISM}
    assert not any("forward" in column or "return" in column for column in evidence.columns)
    assert (features["observed_members"] == 20).all()
