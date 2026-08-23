from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from czsc_trader.baselines import promote_baseline, resolve_baseline


VALID_RULE = {
    "weights": [0.3, 0.3, 0.4],
    "enter": 0.15,
    "exit": 0.0,
    "confirm_days": 1,
    "min_hold_days": 3,
    "exit_confirm_days": 1,
    "entry_gate": "none",
}


def _canonical_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _write_registry_fixture(root: Path) -> Path:
    root.mkdir(parents=True)
    rule_path = root / "baseline_v001.json"
    rule_path.write_text(json.dumps(VALID_RULE, indent=2), encoding="utf-8")
    digest = _canonical_hash(VALID_RULE)
    registry = {
        "schema_version": 1,
        "latest": "baseline_v001",
        "baselines": {
            "baseline_v001": {
                "file": rule_path.name,
                "sha256": digest,
                "source_output": "outputs/source_R01",
                "frozen_at_utc": "2026-08-23T00:00:00+00:00",
                "strategy": "czsc_fixed_rule",
                "required_frequencies": ["30m", "daily", "weekly"],
            }
        },
    }
    (root / "registry.json").write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return root


def test_resolve_latest_baseline_verifies_hash(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")

    resolved = resolve_baseline(root)

    assert resolved.version == "baseline_v001"
    assert resolved.rule.weights == (0.3, 0.3, 0.4)
    assert resolved.source_output == "outputs/source_R01"


def test_resolve_baseline_rejects_modified_rule(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    (root / "baseline_v001.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        resolve_baseline(root)


def test_resolve_baseline_rejects_invalid_rule(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    rule_path = root / "baseline_v001.json"
    invalid = {**VALID_RULE, "weights": [0.3, 0.3]}
    rule_path.write_text(json.dumps(invalid), encoding="utf-8")
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    registry["baselines"]["baseline_v001"]["sha256"] = _canonical_hash(invalid)
    (root / "registry.json").write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="three weights"):
        resolve_baseline(root)


def test_promote_adds_next_immutable_version_and_updates_latest(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    selected = tmp_path / "selected_rule.json"
    selected.write_text(json.dumps(VALID_RULE), encoding="utf-8")

    promoted = promote_baseline(
        root,
        selected,
        "outputs/example_R02",
        datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc),
    )

    assert promoted.version == "baseline_v002"
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    assert registry["latest"] == "baseline_v002"
    assert (root / "baseline_v001.json").is_file()
    assert (root / "baseline_v002.json").is_file()
