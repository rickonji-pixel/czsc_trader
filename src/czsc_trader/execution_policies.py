"""Immutable execution-policy registry bound to one signal baseline."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .identity import canonical_json_sha256


_VERSION_PATTERN = re.compile(r"execution_policy_(\d{8})$")


@dataclass(frozen=True)
class ResolvedExecutionPolicy:
    version: str
    status: str
    symbol: str
    baseline_version: str
    baseline_sha256: str
    family: str
    parameter: float
    atr_window: int
    tick: float
    fee_rate: float
    warning_gap_q05: float
    entry_order_type: str
    exit_limit_ratio: float
    exit_price_rounding: str
    exit_primary_order_type: str
    exit_continuous_fallback: str
    sha256: str
    source_path: str
    source_sha256: str


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read execution policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"execution policy {path} must be an object")
    return payload


def resolve_execution_policy(
    root: Path,
    version: str | None = None,
    *,
    symbol: str | None = None,
    baseline_version: str | None = None,
    baseline_sha256: str | None = None,
    required: bool = False,
) -> ResolvedExecutionPolicy | None:
    """Resolve and verify an active policy; return None for a non-matching optional lookup."""
    root = Path(root)
    registry_path = root / "registry.json"
    if not registry_path.is_file():
        if required:
            raise ValueError(f"missing execution policy registry: {registry_path}")
        return None
    registry = _read_json(registry_path)
    selected = str(version or registry.get("latest", ""))
    if not _VERSION_PATTERN.fullmatch(selected):
        raise ValueError(f"invalid execution policy version: {selected!r}")
    policies = registry.get("policies")
    if not isinstance(policies, dict) or selected not in policies:
        raise ValueError(f"unknown execution policy: {selected}")
    entry = policies[selected]
    if not isinstance(entry, dict):
        raise ValueError(f"invalid execution policy registry entry: {selected}")
    policy_path = root / str(entry.get("file", ""))
    digest = canonical_json_sha256(policy_path) if policy_path.is_file() else ""
    if digest != str(entry.get("sha256", "")).lower():
        raise ValueError(f"{selected}: policy SHA-256 differs from registry")
    payload = _read_json(policy_path)
    baseline = payload.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError(f"{selected}: baseline identity is missing")
    policy_symbol = str(payload.get("symbol", "")).upper()
    policy_baseline = str(baseline.get("version", ""))
    policy_baseline_sha = str(baseline.get("sha256", "")).lower()
    matches = (
        (symbol is None or policy_symbol == str(symbol).upper())
        and (baseline_version is None or policy_baseline == baseline_version)
        and (baseline_sha256 is None or policy_baseline_sha == baseline_sha256.lower())
    )
    if not matches:
        if required:
            raise ValueError("active execution policy does not match symbol and signal baseline")
        return None
    source_path = str(entry.get("source_path", ""))
    source_file = root.parent.parent / source_path
    source_digest = canonical_json_sha256(source_file) if source_file.is_file() else ""
    if source_digest != str(entry.get("source_sha256", "")).lower():
        raise ValueError(f"{selected}: source SHA-256 differs from registry")
    if str(payload.get("version", "")) != selected:
        raise ValueError(f"{selected}: version differs from file")
    return ResolvedExecutionPolicy(
        version=selected,
        status=str(entry.get("status", "")),
        symbol=policy_symbol,
        baseline_version=policy_baseline,
        baseline_sha256=policy_baseline_sha,
        family=str(payload.get("family", "")),
        parameter=float(payload.get("parameter")),
        atr_window=int(payload.get("atr_window")),
        tick=float(payload.get("tick")),
        fee_rate=float(payload.get("fee_rate")),
        warning_gap_q05=float(payload.get("warning_gap_q05")),
        entry_order_type=str(payload.get("entry_order_type", "")),
        exit_limit_ratio=float(payload.get("exit_limit_ratio")),
        exit_price_rounding=str(payload.get("exit_price_rounding", "")),
        exit_primary_order_type=str(payload.get("exit_primary_order_type", "")),
        exit_continuous_fallback=str(payload.get("exit_continuous_fallback", "")),
        sha256=digest,
        source_path=source_path,
        source_sha256=source_digest,
    )
