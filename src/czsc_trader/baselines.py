"""Immutable fixed-rule baseline registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from .walk_forward import Rule


_VERSION_PATTERN = re.compile(r"baseline_(\d{8})$")
_ENTRY_GATES = {"none", "structure", "trend", "structure_and_trend"}


@dataclass(frozen=True)
class ResolvedBaseline:
    version: str
    rule: Rule
    rule_payload: dict[str, object]
    sha256: str
    source_output: str


def _validate_version(version: str) -> None:
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"Invalid rule baseline version {version!r}; expected baseline_YYYYMMDD")
    try:
        datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid rule baseline version {version!r}; expected a real date in baseline_YYYYMMDD"
        ) from exc


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _canonical_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _parse_rule(payload: dict[str, object]) -> Rule:
    required = {
        "weights",
        "enter",
        "exit",
        "confirm_days",
        "min_hold_days",
        "exit_confirm_days",
        "entry_gate",
    }
    missing = sorted(required.difference(payload))
    if missing:
        raise ValueError(f"baseline rule missing fields: {missing}")
    weights_value = payload["weights"]
    if not isinstance(weights_value, list) or len(weights_value) != 3:
        raise ValueError("baseline rule must contain exactly three weights")
    weights = tuple(float(value) for value in weights_value)
    if any(value < 0 for value in weights) or abs(sum(weights) - 1.0) > 1e-9:
        raise ValueError("baseline rule weights must be non-negative and sum to 1")
    enter = float(payload["enter"])
    exit_ = float(payload["exit"])
    if exit_ >= enter:
        raise ValueError("baseline rule exit must be lower than enter")
    confirm_days = int(payload["confirm_days"])
    min_hold_days = int(payload["min_hold_days"])
    exit_confirm_days = int(payload["exit_confirm_days"])
    if min(confirm_days, min_hold_days, exit_confirm_days) < 1:
        raise ValueError("baseline rule day counts must be positive integers")
    gate = str(payload["entry_gate"])
    if gate not in _ENTRY_GATES:
        raise ValueError(f"baseline rule has unknown entry gate: {gate}")
    return Rule(
        weights=weights,
        enter=enter,
        exit=exit_,
        confirm_days=confirm_days,
        min_hold_days=min_hold_days,
        exit_confirm_days=exit_confirm_days,
        entry_gate=gate,
    )


def resolve_baseline(root: Path, version: str | None = None) -> ResolvedBaseline:
    """Resolve and verify one immutable baseline."""
    root = Path(root)
    registry = _load_json_object(root / "registry.json")
    selected_version = str(version or registry.get("latest", ""))
    _validate_version(selected_version)
    baselines = registry.get("baselines")
    if not isinstance(baselines, dict) or selected_version not in baselines:
        raise ValueError(f"Unknown rule baseline: {selected_version}")
    entry = baselines[selected_version]
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid registry entry for {selected_version}")
    filename = str(entry.get("file", ""))
    expected_filename = f"{selected_version}.json"
    if filename != expected_filename:
        raise ValueError(f"{selected_version} must use file {expected_filename}")
    rule_path = root / filename
    if not rule_path.is_file():
        raise ValueError(f"Missing baseline file: {rule_path}")
    payload = _load_json_object(rule_path)
    digest = _canonical_sha256(payload)
    expected = str(entry.get("sha256", ""))
    if digest != expected:
        raise ValueError(f"{selected_version}: SHA-256 differs from registry")
    return ResolvedBaseline(
        version=selected_version,
        rule=_parse_rule(payload),
        rule_payload=payload,
        sha256=digest,
        source_output=str(entry.get("source_output", "")),
    )


def promote_baseline(
    root: Path,
    selected_rule: Path,
    source_output: str,
    now: datetime,
) -> ResolvedBaseline:
    """Explicitly freeze a selected rule as the next baseline version."""
    root = Path(root)
    registry_path = root / "registry.json"
    registry = _load_json_object(registry_path)
    baselines = registry.get("baselines")
    if not isinstance(baselines, dict):
        raise ValueError("registry baselines must be an object")
    payload = _load_json_object(Path(selected_rule))
    _parse_rule(payload)
    version = f"baseline_{now.strftime('%Y%m%d')}"
    rule_path = root / f"{version}.json"
    if version in baselines or rule_path.exists():
        raise FileExistsError(f"Baseline already exists: {rule_path}")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    rule_path.write_text(text, encoding="utf-8")
    digest = _canonical_sha256(payload)
    baselines[version] = {
        "file": rule_path.name,
        "sha256": digest,
        "source_output": source_output,
        "frozen_at_utc": now.isoformat(),
        "strategy": "czsc_fixed_rule",
        "required_frequencies": ["30m", "daily", "weekly"],
    }
    registry["latest"] = version
    temporary = registry_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(registry_path)
    return resolve_baseline(root, version)
