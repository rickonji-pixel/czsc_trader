from __future__ import annotations

import czsc

from czsc_trader.czsc_route import (
    classify_signal_family,
    inventory_records,
    parse_signal_value,
    probe_signal_frequencies,
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
