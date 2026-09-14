from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from czsc_trader.feature_mining import (
    TsfreshFeatureSpec,
    TsfreshIntegrationError,
    extract_causal_rolling_features,
    screen_relevant_features,
)


def _market_frame(rows: int = 16) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "dt": pd.date_range("2026-01-05", periods=rows, freq="B"),
            "return": np.linspace(-0.02, 0.03, rows),
            "volume_change": np.cos(np.arange(rows) / 2),
        }
    )


def test_tsfresh_extracts_causal_fixed_window_features() -> None:
    frame = _market_frame()
    spec = TsfreshFeatureSpec("dt", ("return", "volume_change"), lookback=5)

    result = extract_causal_rolling_features(frame, spec)

    assert len(result.features) == 12
    assert result.features.shape[1] == 20
    assert result.features.index[0] == frame.loc[4, "dt"]
    assert result.features.loc[frame.loc[4, "dt"], "tsfresh__return__mean"] == pytest.approx(
        frame.loc[:4, "return"].mean()
    )
    assert result.evidence["causality_contract"] == "TRAILING_WINDOW_ENDS_AT_FEATURE_TIME"
    assert result.evidence["evidence_scope"] == "DEVELOPMENT_ONLY"

    changed = frame.copy()
    changed.loc[12:, "return"] = 99.0
    rerun = extract_causal_rolling_features(changed, spec)
    cutoff = frame.loc[11, "dt"]
    pd.testing.assert_frame_equal(
        result.features.loc[:cutoff],
        rerun.features.loc[:cutoff],
    )


def test_tsfresh_rejects_ambiguous_or_excessive_input() -> None:
    frame = _market_frame()
    duplicated = frame.copy()
    duplicated.loc[4, "dt"] = duplicated.loc[3, "dt"]
    with pytest.raises(TsfreshIntegrationError, match="unique and increasing"):
        extract_causal_rolling_features(
            duplicated,
            TsfreshFeatureSpec("dt", ("return",), lookback=5),
        )

    with pytest.raises(TsfreshIntegrationError, match="safety limit"):
        extract_causal_rolling_features(
            frame,
            TsfreshFeatureSpec(
                "dt",
                ("return",),
                lookback=5,
                max_expanded_rows=10,
            ),
        )


def test_tsfresh_screen_returns_full_relevance_ledger() -> None:
    index = pd.date_range("2024-01-02", periods=80, freq="B", name="dt")
    target = pd.Series(np.arange(80, dtype=float), index=index, name="forward_return")
    features = pd.DataFrame(
        {
            "strong": target.to_numpy() + np.sin(np.arange(80)) * 0.01,
            "flat": np.resize([0.0, 1.0, -1.0, 0.5], 80),
        },
        index=index,
    )

    result = screen_relevant_features(features, target, ml_task="regression", n_jobs=0)

    assert set(result.relevance["feature"]) == {"strong", "flat"}
    assert "strong" in result.selected_features
    assert result.evidence["method"] == "UNIVARIATE_BENJAMINI_HOCHBERG"
    assert result.evidence["input_feature_count"] == 2

    with pytest.raises(TsfreshIntegrationError, match="indices must match exactly"):
        screen_relevant_features(features, target.iloc[::-1])
