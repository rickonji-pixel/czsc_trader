from __future__ import annotations

import numpy as np
import pandas as pd

from czsc_trader.basis_style_events import MECHANISM, generate_basis_confirmation_events


def test_basis_confirmation_events_are_causal_and_return_free() -> None:
    dates = pd.bdate_range("2025-01-02", periods=240)
    features = pd.DataFrame(
        {
            "dt": dates,
            "relative_strength_5": np.sin(np.arange(len(dates)) / 5),
            "clean_20_session_window": True,
        }
    )
    basis = pd.DataFrame(
        {
            "dt": dates,
            "raw_basis": 0.01 * np.cos(np.arange(len(dates)) / 7),
            "mapping_ts_code": [f"IC{index // 20:02d}.CFX" for index in range(len(dates))],
            "available_for_strategy_from": pd.Series(dates).shift(-1),
        }
    )

    events, density, evidence = generate_basis_confirmation_events(
        features, basis, evaluation_start=dates[120]
    )

    assert (events["event_date"] > events["signal_date"]).all()
    assert set(density["mechanism"]) == {MECHANISM}
    assert not any("forward" in column or "return" in column for column in evidence.columns)
    roll_rows = evidence["mapping_ts_code"].ne(evidence["mapping_ts_code"].shift(1))
    assert (evidence.loc[roll_rows, "basis_change_usable"] == 0.0).all()
