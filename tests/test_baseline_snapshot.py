from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re


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


def test_portable_snapshot_matches_latest_registered_baseline() -> None:
    registry = _load(REGISTRY_PATH)
    version = registry["latest"]
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
