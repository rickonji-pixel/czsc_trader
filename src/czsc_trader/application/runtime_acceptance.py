"""Pre-freeze acceptance for one immutable strategy runtime release."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace

from strategy_manager import StrategyVersion, canonical_sha256
from strategy_runtime import StrategyLoader, StrategyRelease


def _plain(value):
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def prospective_release(version: StrategyVersion) -> StrategyRelease:
    """Build the exact frozen release identity without mutating SM state."""

    release_hash = version.release_hash or canonical_sha256(version.release_payload())
    frozen = replace(version, release_hash=release_hash)
    return StrategyRelease.from_mapping(frozen.to_dict())


def validate_runtime_readiness(version: StrategyVersion) -> dict[str, object]:
    """Prove that the prospective frozen payload has a loadable SRT closure."""

    release = prospective_release(version)
    strategy = StrategyLoader().load(release)
    definition = strategy.definition
    return {
        "schema_version": 1,
        "status": "PASS",
        "release_id": release.release_id,
        "release_hash": release.release_hash,
        "runtime_sha256": definition.runtime_sha256,
        "implementation": {
            "module": definition.implementation.module,
            "qualname": definition.implementation.qualname,
            "contract_version": definition.implementation.contract_version,
            "source_sha256": definition.implementation.source_sha256,
        },
        "input_contract_sha256": canonical_sha256(
            [asdict(item) for item in definition.inputs.requirements]
        ),
        "input_contract": {
            "requirements": [asdict(item) for item in definition.inputs.requirements],
        },
        "decision_contract_sha256": canonical_sha256(asdict(definition.decision)),
        "decision_contract": asdict(definition.decision),
        "execution_policy_sha256": canonical_sha256(
            {
                "policy_type": definition.execution.policy_type,
                "settings": _plain(definition.execution.settings),
            }
        ),
        "execution_policy": {
            "policy_type": definition.execution.policy_type,
            "settings": _plain(definition.execution.settings),
        },
        "state_mode": definition.state_mode,
    }


def require_runtime_readiness(
    version: StrategyVersion,
    validator=validate_runtime_readiness,
) -> dict[str, object]:
    """Reject false-success readiness reports before SM freezes a version."""

    report = validator(version)
    release = prospective_release(version)
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise ValueError("strategy runtime readiness did not pass")
    if report.get("release_id") != release.release_id:
        raise ValueError("strategy runtime readiness belongs to another release")
    if report.get("release_hash") != release.release_hash:
        raise ValueError("strategy runtime readiness release hash differs")
    runtime_sha256 = report.get("runtime_sha256")
    if not isinstance(runtime_sha256, str) or len(runtime_sha256) != 64:
        raise ValueError("strategy runtime readiness is missing runtime identity")
    return report
