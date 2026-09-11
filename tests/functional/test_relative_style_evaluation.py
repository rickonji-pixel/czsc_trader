from __future__ import annotations

import numpy as np
import pandas as pd

from czsc_trader.relative_style_evaluation import evaluate_relative_style_events


def _five_minute(days: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    clocks = ("09:35", "09:40", "10:30", "10:35", "11:30", "13:05", "15:00")
    for day_index, day in enumerate(days):
        base = 10 + day_index * 0.01
        for clock in clocks:
            close = base * (1.01 if clock in {"11:30", "15:00"} else 1.0)
            rows.append(
                {
                    "Date": pd.Timestamp(f"{day.date()} {clock}"),
                    "Open": base,
                    "Close": close,
                }
            )
    return pd.DataFrame(rows)


def test_relative_style_evaluation_compares_frozen_controls() -> None:
    days = pd.bdate_range("2025-01-02", periods=280)
    selected = days[::4]
    events = pd.concat(
        [
            pd.DataFrame(
                {
                    "mechanism": mechanism,
                    "signal_date": selected - pd.offsets.BDay(1),
                    "event_date": selected,
                }
            )
            for mechanism in ("A", "B")
        ],
        ignore_index=True,
    )
    mechanisms = {
        "A": {"entry_checkpoint": "OPEN", "exit_checkpoint": "11:30_CLOSE"},
        "B": {"entry_checkpoint": "09:40_OPEN", "exit_checkpoint": "11:30_CLOSE"},
    }
    result = evaluate_relative_style_events(
        events,
        _five_minute(days),
        mechanisms,
        evaluation_start=days[0],
        baseline_one_way_cost=0.00012,
        stress_one_way_cost=0.0006,
        base_fraction=0.5,
        positive_years_required=1,
        recent_sessions=60,
    )

    assert set(result.metrics["mechanism"]) == {"A", "B"}
    assert set(result.episodes["variant"]) == {"PRIMARY_LONG", "OPPOSITE_SHORT"}
    assert result.metrics["stress_mean_return"].gt(0).all()
    assert np.isfinite(result.account["max_drawdown"]).all()
