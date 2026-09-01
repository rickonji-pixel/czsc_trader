from __future__ import annotations

import czsc
import numpy as np
import pandas as pd
import pytest

from czsc_trader.czsc_route import (
    admit_factors,
    build_family_factors,
    classify_signal_family,
    inventory_records,
    parse_signal_value,
    probe_signal_frequencies,
    replay_family_factors,
    validate_route_family_protocol,
)


def test_parse_signal_value_preserves_three_semantic_fields() -> None:
    parsed = parse_signal_value("向上_第3层_强_任意")

    assert parsed.primary == "向上"
    assert parsed.secondary == "第3层"
    assert parsed.tertiary == "强"
    assert parsed.original == "向上_第3层_强_任意"


def test_parse_signal_value_pads_missing_fields_without_inventing_values() -> None:
    parsed = parse_signal_value("二卖")

    assert (parsed.primary, parsed.secondary, parsed.tertiary) == ("二卖", None, None)
    assert parsed.original == "二卖"


def test_signal_family_classification_separates_structure_from_hybrid_ta() -> None:
    assert classify_signal_family("cxt_bi_zdf_V230601").family == "atomic_structure"
    assert classify_signal_family("cxt_second_bs_V230320").family == "sparse_event"
    assert classify_signal_family("byi_symmetry_zs_V221107").family == "atomic_structure"
    assert classify_signal_family("zdy_zs_V230423").family == "atomic_structure"

    hybrid = classify_signal_family("zdy_macd_V230518")
    assert hybrid.status == "excluded"
    assert hybrid.reason == "hybrid_ta_not_pure_czsc_structure"

    non_structure = classify_signal_family("tas_macd_base_V221028")
    assert non_structure.status == "excluded"
    assert non_structure.reason == "non_czsc_structure_signal"


def test_inventory_accounts_for_every_native_signal_deterministically() -> None:
    names = list(czsc._native.list_signal_names())
    first = inventory_records(names)
    second = inventory_records(reversed(names))

    assert first == second
    assert [row["name"] for row in first] == sorted(names)
    assert len(first) == len(names)
    assert all(row["status"] in {"candidate", "excluded"} for row in first)
    assert all(row["family"] or row["reason"] for row in first)
    assert {row["name"] for row in first} == set(names)


def test_frequency_probe_records_success_and_explicit_failure() -> None:
    records = inventory_records(["cxt_bi_status_V230101", "tas_macd_base_V221028"])

    def execute(name: str, frequency: str) -> str:
        if frequency == "周线":
            raise ValueError("insufficient bars")
        return f"{name}:{frequency}"

    probes = probe_signal_frequencies(records, execute)

    assert len(probes) == 3
    assert [row["frequency"] for row in probes] == ["30分钟", "日线", "周线"]
    assert [row["status"] for row in probes] == ["generated", "generated", "unavailable"]
    assert probes[-1]["error"] == "ValueError: insufficient bars"


def test_family_builder_preserves_individual_and_joint_signal_values() -> None:
    index = pd.date_range("2021-01-01", periods=4, freq="D")
    raw = pd.DataFrame(
        {"raw__daily__cxt_demo": ["向上_第1层_强", "向上_第2层_弱", "向下_第1层_强", "其他_任意_任意"]},
        index=index,
    )

    factors, records = build_family_factors(raw, "atomic_structure", {})

    assert "state__raw__daily__cxt_demo__v1::向上" in factors
    assert {row["representation"] for row in records} == {"v1", "v2", "v3", "joint"}
    assert any(row["representation"] == "v3" and row["value"] == "强" for row in records)
    assert any(
        row["representation"] == "joint" and row["value"] == "向上|第1层|强"
        for row in records
    )

    canonical = [row for row in records if row["status"] == "candidate"]
    replay = replay_family_factors(raw.iloc[:2], canonical)
    assert list(replay.columns) == [row["factor"] for row in canonical]
    assert replay.equals(factors.iloc[:2])


def _admission_fixture() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, object]]:
    dates = pd.DatetimeIndex(
        [pd.Timestamp(year=year, month=1, day=day) for year in range(2021, 2026) for day in range(1, 21)],
        name="dt",
    )
    active = np.tile([1.0] * 10 + [0.0] * 10, 5)
    reversed_state = active.copy()
    reversed_state[-40:] = 1.0 - reversed_state[-40:]
    event = np.zeros(len(dates))
    event[[offset for year in range(5) for offset in (year * 20, year * 20 + 4)]] = 1.0
    duplicate = np.tile([1.0, 0.0], 50)
    candidates = pd.DataFrame(
        {
            "stable_state": active,
            "reversed_state": reversed_state,
            "stable_event": event,
            "duplicate_reference": duplicate,
        },
        index=dates,
    )
    returns = np.where(active == 1.0, 0.02, -0.01)
    returns[np.flatnonzero(event)] = 0.04
    outcomes = pd.DataFrame(
        {"return_20": returns, "max_drawdown_20": -0.02},
        index=dates,
    )
    references = pd.DataFrame(
        {"target_position": 1.0, "champion_duplicate": duplicate},
        index=dates,
    )
    protocol = {
        "primary_horizon": 20,
        "minimum_absolute_effect": 0.005,
        "state_min_active_days_per_year": 10,
        "state_min_control_days_per_year": 10,
        "state_max_active_ratio": 0.9,
        "state_min_same_direction_years": 4,
        "event_min_independent_occurrences": 10,
        "event_min_years": 4,
        "event_min_occurrences_per_supported_year": 2,
        "maximum_annual_absolute_effect_share": 0.5,
        "minimum_event_leave_one_out_effect_ratio": 0.5,
        "conditional_reference_min_abs_correlation": 0.8,
    }
    return candidates, outcomes, references, protocol


def test_common_admission_rejects_reversal_outlier_and_duplicate() -> None:
    candidates, outcomes, references, protocol = _admission_fixture()
    kinds = {
        "stable_state": "state",
        "reversed_state": "state",
        "stable_event": "event",
        "duplicate_reference": "state",
    }

    result = admit_factors(
        candidates,
        references,
        outcomes,
        protocol,
        factor_kinds=kinds,
        causal_passed=set(candidates.columns),
    )

    assert "stable_state" in set(result.admitted["factor"])
    rejected = result.rejected.set_index("factor")["reason"].to_dict()
    assert rejected["reversed_state"] == "cross_year_direction"
    assert rejected["duplicate_reference"] == "exact_reference_duplicate"


def test_common_admission_rejects_single_outlier_driven_event() -> None:
    dates = pd.DatetimeIndex(
        [pd.Timestamp(year=year, month=1, day=day) for year in range(2021, 2026) for day in range(1, 21)],
        name="dt",
    )
    event = np.zeros(len(dates))
    positions = [offset for year in range(5) for offset in (year * 20, year * 20 + 5)]
    event[positions] = 1.0
    returns = np.zeros(len(dates))
    returns[positions] = 0.006
    returns[positions[0]] = 0.20
    candidates = pd.DataFrame({"outlier_event": event}, index=dates)
    outcomes = pd.DataFrame({"return_20": returns, "max_drawdown_20": 0.0}, index=dates)
    references = pd.DataFrame({"target_position": 1.0}, index=dates)
    _, _, _, protocol = _admission_fixture()

    result = admit_factors(
        candidates,
        references,
        outcomes,
        protocol,
        factor_kinds={"outlier_event": "event"},
        causal_passed={"outlier_event"},
    )

    assert result.admitted.empty
    assert result.rejected.set_index("factor").loc["outlier_event", "reason"] == "event_influence"


def test_route_family_protocol_rejects_mutable_or_future_scope() -> None:
    valid = {
        "experiment_id": "0901_EX06",
        "handler": "czsc_route_family_diagnostic",
        "symbol": "588080.SH",
        "champion": {
            "version": "baseline_20260826",
            "sha256": "fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822",
        },
        "family": "atomic_structure",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "inventory_sha256": "0baf02a789774a33ba376eabe33030327bf3f6584382db2d71b9c75c4753fd32",
        "signal_names": ["cxt_bi_status_V230101"],
        "frequencies": ["30分钟", "日线", "周线"],
    }
    validate_route_family_protocol(valid)

    for key, value in (
        ("family", "unknown"),
        ("visible_end", "2026-01-01"),
        ("access_2026", True),
        ("inventory_sha256", "changed"),
    ):
        changed = {**valid, key: value}
        with pytest.raises(ValueError):
            validate_route_family_protocol(changed)
