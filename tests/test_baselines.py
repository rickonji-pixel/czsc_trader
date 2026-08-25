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
    rule_path = root / "baseline_20260822.json"
    rule_path.write_text(json.dumps(VALID_RULE, indent=2), encoding="utf-8")
    digest = _canonical_hash(VALID_RULE)
    registry = {
        "schema_version": 1,
        "latest": "baseline_20260822",
        "baselines": {
            "baseline_20260822": {
                "file": rule_path.name,
                "sha256": digest,
                "verification_snapshot": "docs/baselines/example_expected.json",
                "frozen_at_utc": "2026-08-23T00:00:00+00:00",
                "strategy": "czsc_fixed_rule",
                "required_frequencies": ["30m", "daily", "weekly"],
            }
        },
    }
    (root / "registry.json").write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return root


def _four_layer_payload() -> dict[str, object]:
    names = [f"raw__daily__factor_{index:02d}" for index in range(12)]
    values = [0.05] * 10 + [0.25, 0.25]
    return {
        "schema_version": 1,
        "experiment": "0824_EX04",
        "sample_end": "2025-12-31",
        "champion": {
            "version": "baseline_20260823",
            "sha256": _canonical_hash(VALID_RULE),
        },
        "factor_policy": "exact_champion_signals_no_add_drop_or_reselection",
        "factor_names": names,
        "spec": {
            "step": 0.0125,
            "rounds": 1,
            "allow_sign_flip": False,
            "enter": 0.175,
            "exit": 0.025,
        },
        "spec_id": "test",
        "weights": dict(zip(names, values, strict=True)),
        "selection_metric": "strategy_return_only",
    }


def _write_four_layer_registry_fixture(repository: Path) -> Path:
    root = repository / "configs" / "rule_baselines"
    source = repository / "experiments" / "0824_EX04" / "artifacts" / "frozen_challenger.json"
    root.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    old_path = root / "baseline_20260823.json"
    old_path.write_text(json.dumps(VALID_RULE, indent=2), encoding="utf-8")
    frozen = _four_layer_payload()
    source_bytes = (json.dumps(frozen, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    source.write_bytes(source_bytes)
    (root / "baseline_20260826.json").write_bytes(source_bytes)
    registry = {
        "schema_version": 3,
        "latest": "baseline_20260826",
        "baselines": {
            "baseline_20260823": {
                "file": "baseline_20260823.json",
                "sha256": _canonical_hash(VALID_RULE),
                "verification_snapshot": "docs/baselines/588080_2026_expected.json",
                "frozen_at_utc": "2026-08-23T00:00:00+00:00",
                "strategy": "czsc_fixed_rule",
                "required_frequencies": ["30m", "daily", "weekly"],
                "status": "archived",
                "scope": "historical_generic",
            },
            "baseline_20260826": {
                "file": "baseline_20260826.json",
                "sha256": _canonical_hash(frozen),
                "verification_snapshot": "",
                "frozen_at_utc": "2026-08-26T00:00:00+00:00",
                "strategy": "czsc_four_layer",
                "required_frequencies": ["30m", "daily", "weekly"],
                "status": "active",
                "scope": "symbol",
                "symbol": "588080.SH",
                "source_path": "experiments/0824_EX04/artifacts/frozen_challenger.json",
                "source_sha256": sha256(source_bytes).hexdigest(),
                "selection_sample_end": "2025-12-31",
                "forward_validation_start": "2026-08-26",
            },
        },
    }
    (root / "registry.json").write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return root


def test_resolve_latest_baseline_verifies_hash(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")

    resolved = resolve_baseline(root)

    assert resolved.version == "baseline_20260822"
    assert resolved.rule.weights == (0.3, 0.3, 0.4)
    assert resolved.verification_snapshot == "docs/baselines/example_expected.json"


def test_resolve_baseline_rejects_modified_rule(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    (root / "baseline_20260822.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        resolve_baseline(root)


def test_resolve_baseline_rejects_invalid_rule(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    rule_path = root / "baseline_20260822.json"
    invalid = {**VALID_RULE, "weights": [0.3, 0.3]}
    rule_path.write_text(json.dumps(invalid), encoding="utf-8")
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    registry["baselines"]["baseline_20260822"]["sha256"] = _canonical_hash(invalid)
    (root / "registry.json").write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="three weights"):
        resolve_baseline(root)


def test_resolve_baseline_rejects_non_date_version(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    registry_path = root / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["latest"] = "baseline_v001"
    registry["baselines"]["baseline_v001"] = registry["baselines"].pop(
        "baseline_20260822"
    )
    registry["baselines"]["baseline_v001"]["file"] = "baseline_v001.json"
    (root / "baseline_20260822.json").rename(root / "baseline_v001.json")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="baseline_YYYYMMDD"):
        resolve_baseline(root)


def test_resolve_baseline_rejects_filename_that_differs_from_version(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    registry_path = root / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["baselines"]["baseline_20260822"]["file"] = "other.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="must use file baseline_20260822.json"):
        resolve_baseline(root)


def test_promote_adds_date_version_and_updates_latest(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    selected = tmp_path / "selected_rule.json"
    selected.write_text(json.dumps(VALID_RULE), encoding="utf-8")

    promoted = promote_baseline(
        root,
        selected,
        "docs/baselines/example_expected.json",
        datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc),
    )

    assert promoted.version == "baseline_20260823"
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    assert registry["latest"] == "baseline_20260823"
    assert (root / "baseline_20260822.json").is_file()
    assert (root / "baseline_20260823.json").is_file()


def test_promote_rejects_second_baseline_on_same_date(tmp_path: Path) -> None:
    root = _write_registry_fixture(tmp_path / "baselines")
    selected = tmp_path / "selected_rule.json"
    selected.write_text(json.dumps(VALID_RULE), encoding="utf-8")
    now = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
    promote_baseline(root, selected, "docs/baselines/example_expected.json", now)

    with pytest.raises(FileExistsError, match="baseline_20260823"):
        promote_baseline(root, selected, "docs/baselines/example_expected.json", now)


def test_resolve_active_four_layer_baseline_with_archived_state_machine(
    tmp_path: Path,
) -> None:
    root = _write_four_layer_registry_fixture(tmp_path / "repository")

    resolved = resolve_baseline(root, symbol="588080.SH")

    assert resolved.version == "baseline_20260826"
    assert resolved.strategy == "czsc_four_layer"
    assert resolved.status == "active"
    assert resolved.scope == "symbol"
    assert resolved.symbol == "588080.SH"
    assert resolved.factor_names == tuple(_four_layer_payload()["factor_names"])
    assert resolved.rule.enter == pytest.approx(0.175)
    assert resolved.rule.exit == pytest.approx(0.025)
    assert resolved.rule.min_hold_days == VALID_RULE["min_hold_days"]
    assert sum(abs(value) for value in resolved.factor_weights) == pytest.approx(1.0)
    assert resolved.forward_validation_start == "2026-08-26"


def test_scoped_active_baseline_rejects_other_symbol() -> None:
    with pytest.raises(ValueError, match="588080.SH"):
        resolve_baseline(
            Path("configs/rule_baselines"),
            "baseline_20260826",
            symbol="600519.SH",
        )


def test_archived_baseline_is_explicit_only(tmp_path: Path) -> None:
    root = _write_four_layer_registry_fixture(tmp_path / "repository")
    registry_path = root / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["latest"] = "baseline_20260823"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="archived.*explicit"):
        resolve_baseline(root, symbol="588080.SH")

    resolved = resolve_baseline(root, "baseline_20260823", symbol="588080.SH")
    assert resolved.status == "archived"


def test_four_layer_source_identity_and_copy_equality_are_enforced(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    root = _write_four_layer_registry_fixture(repository)
    source = repository / "experiments" / "0824_EX04" / "artifacts" / "frozen_challenger.json"
    source.write_bytes(source.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="source SHA-256"):
        resolve_baseline(root, symbol="588080.SH")

    registry_path = root / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["baselines"]["baseline_20260826"]["source_sha256"] = sha256(
        source.read_bytes()
    ).hexdigest()
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ValueError, match="byte-identical"):
        resolve_baseline(root, symbol="588080.SH")


def test_four_layer_champion_hash_must_match_archived_baseline(tmp_path: Path) -> None:
    root = _write_four_layer_registry_fixture(tmp_path / "repository")
    promoted_path = root / "baseline_20260826.json"
    payload = json.loads(promoted_path.read_text(encoding="utf-8"))
    payload["champion"]["sha256"] = "0" * 64
    changed = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    promoted_path.write_bytes(changed)
    source = tmp_path / "repository" / "experiments" / "0824_EX04" / "artifacts" / "frozen_challenger.json"
    source.write_bytes(changed)
    registry_path = root / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["baselines"]["baseline_20260826"]["sha256"] = _canonical_hash(payload)
    registry["baselines"]["baseline_20260826"]["source_sha256"] = sha256(changed).hexdigest()
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="champion SHA-256"):
        resolve_baseline(root, symbol="588080.SH")
