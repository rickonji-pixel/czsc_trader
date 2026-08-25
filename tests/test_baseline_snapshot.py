from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

import pandas as pd

from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import load_market_data
from czsc_trader.factors import generate_factor_frame
from czsc_trader.four_layer import (
    normalized_signal_factors,
    positions_from_scores,
    score_four_layer,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "configs" / "rule_baselines" / "registry.json"
SNAPSHOT_PATH = ROOT / "docs" / "baselines" / "588080_2026_expected.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def test_portable_snapshot_matches_archived_20260823_baseline() -> None:
    registry = _load(REGISTRY_PATH)
    version = "baseline_20260823"
    entry = registry["baselines"][version]
    rule = _load(REGISTRY_PATH.parent / entry["file"])
    snapshot = _load(SNAPSHOT_PATH)

    assert entry["verification_snapshot"] == "docs/baselines/588080_2026_expected.json"
    assert snapshot["baseline"] == {
        "version": version,
        "sha256": entry["sha256"],
        "rule": rule,
    }
    assert snapshot["baseline"]["sha256"] == _canonical_hash(rule)
    assert entry["status"] == "archived"
    assert registry["latest"] == "baseline_20260826"


def test_promoted_baseline_is_byte_identical_to_ex04_source() -> None:
    registry = _load(REGISTRY_PATH)
    entry = registry["baselines"]["baseline_20260826"]
    promoted = REGISTRY_PATH.parent / entry["file"]
    source = ROOT / entry["source_path"]

    assert promoted.read_bytes() == source.read_bytes()
    assert sha256(source.read_bytes()).hexdigest() == entry["source_sha256"]
    assert _canonical_hash(_load(promoted)) == entry["sha256"]
    assert entry["status"] == "active"
    assert entry["symbol"] == "588080.SH"


def test_promoted_baseline_replays_ex04_execution_on_tracked_selection_data() -> None:
    baseline = resolve_baseline(REGISTRY_PATH.parent, symbol="588080.SH")
    data = load_market_data(
        ROOT / "data" / "raw",
        "588080.SH",
        "etf",
        cutoff=pd.Timestamp("2025-12-31"),
    )
    frame = generate_factor_frame(data).frame
    applied = apply_resolved_baseline(frame, baseline)
    frozen = _load(ROOT / "experiments" / "0824_EX04" / "artifacts" / "frozen_challenger.json")
    names = tuple(map(str, frozen["factor_names"]))
    weights = pd.Series(
        [float(frozen["weights"][name]) for name in names],
        index=names,
        dtype=float,
    )
    factors = normalized_signal_factors(frame.loc[:, list(names)])
    direct_scores = score_four_layer(factors, weights)
    direct_target = positions_from_scores(
        direct_scores,
        float(frozen["spec"]["enter"]),
        float(frozen["spec"]["exit"]),
        baseline.rule,
    )

    pd.testing.assert_series_equal(applied.scores, direct_scores, check_exact=True)
    pd.testing.assert_series_equal(applied.target_position, direct_target, check_exact=True)


def test_portable_snapshot_has_no_machine_local_output_identity() -> None:
    snapshot = _load(SNAPSHOT_PATH)
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert "reference_local_run_label" not in snapshot
    assert "source_output" not in serialized
    assert "outputs/" not in serialized
    assert not re.search(r"\bR\d{2}\b", serialized)
    assert snapshot["required_artifacts"] == [
        "audit.json",
        "baseline_rule.json",
        "chart_2026Q1.html",
        "chart_2026H1.html",
        "chart_2026M1-M8.html",
        "equity_2026Q1.csv",
        "equity_2026H1.csv",
        "equity_2026M1-M8.csv",
        "factor_events.csv",
        "factors.csv",
        "manifest.json",
        "metrics.json",
        "orders_2026Q1.csv",
        "orders_2026H1.csv",
        "orders_2026M1-M8.csv",
        "report.md",
    ]
