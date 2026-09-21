"""Pre-freeze acceptance for one immutable strategy runtime release."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace

from strategy_manager import CandidateSnapshot, StrategyVersion, canonical_sha256
from strategy_runtime import StrategyCandidate, StrategyRelease, StrategyRuntime


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


def _runtime_report(definition) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "PASS",
        "release_id": definition.release_id,
        "release_hash": definition.release_hash,
        "identity_kind": definition.identity_kind,
        "runtime_sha256": definition.runtime_sha256,
        "parameters_sha256": definition.parameters.sha256,
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
        "history_policy_sha256": canonical_sha256(asdict(definition.history)),
        "history_policy": asdict(definition.history),
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
        "capabilities_sha256": canonical_sha256(asdict(definition.capabilities)),
        "monitoring_sha256": canonical_sha256({
            "policy_type": definition.monitoring.policy_type,
            "rules": _plain(definition.monitoring.rules),
        }),
    }


def validate_runtime_readiness(version: StrategyVersion) -> dict[str, object]:
    """Load the exact prospective release without changing SM state."""
    return _runtime_report(StrategyRuntime().describe(prospective_release(version)))


def validate_candidate_readiness(snapshot: CandidateSnapshot) -> dict[str, object]:
    """Gate 2 binds a candidate implementation, never a prospective vN wrapper."""
    candidate = StrategyCandidate(
        snapshot.strategy_id, snapshot.candidate_id, snapshot.strategy_payload,
    )
    report = _runtime_report(StrategyRuntime().describe(candidate))
    report["strategy_payload_hash"] = canonical_sha256(snapshot.strategy_payload)
    return report


def require_same_runtime_content(candidate: dict, release: dict) -> None:
    """Only lifecycle identity may change when a reviewed candidate is frozen."""
    if candidate.get("identity_kind") != "CANDIDATE" or release.get("identity_kind") != "RELEASE":
        raise ValueError("freeze requires candidate-to-release runtime identities")
    for key in (
        "implementation", "parameters_sha256", "strategy_payload_hash",
        "input_contract_sha256", "decision_contract_sha256", "execution_policy_sha256",
        "history_policy_sha256",
        "state_mode", "capabilities_sha256", "monitoring_sha256",
    ):
        if key not in candidate or candidate[key] != release.get(key):
            raise ValueError(f"frozen runtime differs from reviewed candidate: {key}")


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
