from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.dominant_exit_anatomy_runner import (
    classify_anatomy,
    discover_atomic_signatures,
    extract_signal_descriptors,
    render_dominant_event_svg,
    validate_event_cohort,
    validate_protocol,
    validate_trajectory_evidence,
    validate_visible_hashes,
)


DISCOVERY = (
    "2021H1:early_ex04_exit:20210616",
    "2021H2:early_ex04_exit:20210706",
)
CONFIRMATION = "2023H1:early_ex04_exit:20230208"


def _protocol() -> dict[str, object]:
    return {
        "experiment_type": "dominant_exit_path_anatomy",
        "status": "PRE_REGISTERED",
        "visible_sample_end": "2025-12-31",
        "holdout_access_allowed": False,
        "expected_event_count": 20,
        "expected_factor_count": 12,
        "lookback_trading_days": 20,
        "discovery_event_ids": list(DISCOVERY),
        "confirmation_event_id": CONFIRMATION,
        "run_length_bins": [1, 3, 5, 10, 20],
        "transition_age_bins": [1, 3, 5, 10, 20],
        "flip_windows": [5, 10, 20],
        "score_lags": [1, 3, 5],
        "below_threshold_windows": [5, 10],
        "maximum_protective_support": 2,
        "require_zero_joint_margin_protective_support": True,
        "promotion": {
            "optimize_parameters": False,
            "select_candidate": False,
            "write_frozen_challenger": False,
            "run_holdout": False,
            "update_baseline": False,
        },
    }


def test_validate_protocol_rejects_holdout_or_feature_drift() -> None:
    protocol = _protocol()
    validate_protocol(protocol)
    protocol["holdout_access_allowed"] = True
    with pytest.raises(ValueError, match="holdout"):
        validate_protocol(protocol)

    protocol = _protocol()
    protocol["flip_windows"] = [3, 5, 10, 20]
    with pytest.raises(ValueError, match="descriptor"):
        validate_protocol(protocol)


def test_extract_descriptors_uses_only_history_through_signal_date() -> None:
    index = pd.date_range("2025-01-01", periods=8, freq="D")
    raw = pd.DataFrame(
        {
            "signal_a": ["up", "up", "down", "down", "up", "up", "up", "future"],
            "signal_b": ["x", "x", "x", "x", "x", "x", "x", "future"],
            "signal_c": ["bull", "bull", "bull", "bull", "bull", "bull", "bull", "future"],
        },
        index=index,
    )
    mapped = pd.DataFrame(
        {
            "signal_a": [1, 1, -1, -1, 1, 1, 1, -1],
            "signal_b": [0] * 7 + [1],
            "signal_c": [1] * 7 + [-1],
        },
        index=index,
        dtype=float,
    )
    weights = pd.Series({"signal_a": 0.5, "signal_b": 0.25, "signal_c": 0.25})
    contributions = mapped.mul(weights, axis=1)
    score = contributions.sum(axis=1)

    result = extract_signal_descriptors(
        raw,
        mapped,
        contributions,
        score,
        index[6],
        {**_protocol(), "lookback_trading_days": 6},
        exit_threshold=0.025,
        direction_factors=("signal_a", "signal_b", "signal_c"),
    )

    assert result["signal_a|primary_at_t"] == "up"
    assert result["signal_a|mapped_at_t"] == "positive"
    assert result["signal_a|run_length_bin"] == "2_3"
    assert result["signal_a|last_transition"] == "down=>up"
    assert result["signal_a|transition_age_bin"] == "2_3"
    assert result["signal_a|flips_5_bin"] == "2_plus"
    assert result["signal_a|flips_10_bin"] == "2_plus"
    assert result["signal_a|flips_20_bin"] == "2_plus"
    assert result["signal_a|contribution_delta_1_sign"] == "zero"
    assert result["signal_a|contribution_delta_5_sign"] == "zero"
    assert result["signal_b|run_length_bin"] == "6_10"
    assert result["signal_b|last_transition"] == "no_transition"
    assert result["signal_b|transition_age_bin"] == "none"
    assert result["__strategy__|score_delta_1_sign"] == "zero"
    assert result["__strategy__|score_delta_3_sign"] == "positive"
    assert result["__strategy__|score_delta_5_sign"] == "zero"
    assert result["__strategy__|exit_margin_sign"] == "positive"
    assert result["__strategy__|below_exit_5_bin"] == "2_3"
    assert result["__strategy__|direction_alignment"] == "two_same"
    assert all("future" not in str(value) for value in result.values())


def _events() -> pd.DataFrame:
    ids = [*DISCOVERY, CONFIRMATION] + [f"event_{index:02d}" for index in range(17)]
    labels = ["false_exit"] * 5 + ["protective_exit"] * 11 + ["neutral_exit"] * 4
    blocks = ["joint_margin_block"] * 5 + ["joint_margin_block"] * 2 + [
        "independent_double_block"
    ] * 9 + ["joint_margin_block"] * 4
    return pd.DataFrame({"event_id": ids, "outcome_label": labels, "block_label": blocks})


def test_discovery_is_sequential_and_removes_constants() -> None:
    events = _events()
    matrix = pd.DataFrame(
        {
            "factor_a|primary_at_t": ["shared", "shared", "shared"] + ["other"] * 17,
            "factor_b|mapped_at_t": ["discovery", "discovery", "miss"] + ["other"] * 17,
            "factor_c|mapped_at_t": ["constant"] * 20,
        },
        index=events["event_id"],
    )
    discovered, confirmed = discover_atomic_signatures(matrix, events, _protocol())

    assert set(discovered["factor"]) == {"factor_a", "factor_b"}
    assert confirmed[["factor", "descriptor", "value"]].to_dict("records") == [
        {"factor": "factor_a", "descriptor": "primary_at_t", "value": "shared"}
    ]
    row = confirmed.iloc[0]
    assert int(row["protective_support"]) == 0
    assert int(row["joint_margin_protective_support"]) == 0
    assert bool(row["low_contamination"])


def test_contamination_blocks_low_contamination_status() -> None:
    events = _events()
    values = ["shared", "shared", "shared"] + ["other", "other"] + [
        "shared", "shared", "shared"
    ] + ["other"] * 12
    matrix = pd.DataFrame({"factor_a|primary_at_t": values}, index=events["event_id"])
    _, confirmed = discover_atomic_signatures(matrix, events, _protocol())

    row = confirmed.iloc[0]
    assert int(row["protective_support"]) == 3
    assert int(row["joint_margin_protective_support"]) == 2
    assert not bool(row["low_contamination"])


@pytest.mark.parametrize(
    ("event_count", "discovered_rows", "confirmed_rows", "low", "expected"),
    [
        (19, 0, 0, False, "insufficient_anatomy_evidence"),
        (20, 1, 1, True, "shared_confirmed_low_contamination_anatomy"),
        (20, 1, 1, False, "shared_but_contaminated_anatomy"),
        (20, 1, 0, False, "discovery_only_anatomy"),
        (20, 0, 0, False, "idiosyncratic_dominant_events"),
    ],
)
def test_classification_order(
    event_count: int,
    discovered_rows: int,
    confirmed_rows: int,
    low: bool,
    expected: str,
) -> None:
    events = _events().iloc[:event_count].copy()
    discovered = pd.DataFrame(
        [{"factor": "a", "descriptor": "d", "value": "v"}] * discovered_rows
    )
    confirmed = pd.DataFrame(
        [
            {
                "factor": "a",
                "descriptor": "d",
                "value": "v",
                "low_contamination": low,
            }
        ]
        * confirmed_rows
    )

    result = classify_anatomy(events, discovered, confirmed, _protocol())

    assert result["classification"] == expected


def test_event_cohort_rejects_changed_target_identity_or_2026() -> None:
    events = _events()
    events["signal_date"] = pd.date_range("2024-01-01", periods=20, freq="D")
    validate_event_cohort(events, _protocol())

    changed = events.copy()
    changed.loc[0, "event_id"] = "changed"
    with pytest.raises(ValueError, match="dominant event"):
        validate_event_cohort(changed, _protocol())

    leaked = events.copy()
    leaked.loc[0, "signal_date"] = "2026-01-02"
    with pytest.raises(ValueError, match="2026"):
        validate_event_cohort(leaked, _protocol())


def test_visible_hashes_reject_2026_even_when_other_inputs_are_valid() -> None:
    validate_visible_hashes({"588080_daily_2025.csv": "abc"})
    with pytest.raises(AssertionError, match="2026"):
        validate_visible_hashes({"588080_daily_2026.csv": "abc"})


def test_trajectory_validation_rejects_future_rows_and_score_drift() -> None:
    events = _events()
    events["signal_date"] = pd.Timestamp("2025-01-10")
    factor_names = [f"factor_{index}" for index in range(12)]
    rows = []
    for event_id in events["event_id"]:
        for offset, date in [(-1, "2025-01-09"), (0, "2025-01-10")]:
            row = {
                "event_id": event_id,
                "date": date,
                "signal_date": "2025-01-10",
                "offset": offset,
                "ex04_score": 0.02,
                "source_ex04_score": 0.02,
            }
            for factor in factor_names:
                row[f"mapped::{factor}"] = 0.0
            rows.append(row)
    trajectory = pd.DataFrame(rows)
    validate_trajectory_evidence(trajectory, events, factor_names, _protocol())

    future = trajectory.copy()
    future.loc[0, "date"] = "2025-01-11"
    with pytest.raises(ValueError, match="future"):
        validate_trajectory_evidence(future, events, factor_names, _protocol())

    drift = trajectory.copy()
    drift.loc[drift["offset"].eq(0), "source_ex04_score"] = 0.03
    with pytest.raises(AssertionError, match="score"):
        validate_trajectory_evidence(drift, events, factor_names, _protocol())


def test_svg_renderer_escapes_labels_and_uses_only_three_targets() -> None:
    trajectory = pd.DataFrame(
        {
            "event_id": [DISCOVERY[0]] * 2 + [DISCOVERY[1]] * 2 + [CONFIRMATION] * 2,
            "offset": [-1, 0] * 3,
            "normalized_close": [1.0, 1.1, 1.0, 0.9, 1.0, 1.05],
            "ex04_score": [0.1, 0.02, 0.08, 0.01, 0.05, 0.02],
        }
    )
    svg = render_dominant_event_svg(
        trajectory,
        (*DISCOVERY, CONFIRMATION),
        exit_threshold=0.025,
        titles=("A&B", "second", "third"),
    )

    assert svg.startswith("<svg")
    assert "A&amp;B" in svg
    assert svg.count('<line class="signal-day"') == 3
