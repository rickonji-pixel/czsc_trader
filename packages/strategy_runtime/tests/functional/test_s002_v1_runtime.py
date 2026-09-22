from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy_runtime import RuntimeCompatibilityError, RuntimeContractError, StrategyRelease
from strategy_runtime.loader import StrategyLoader


ROOT = Path(__file__).resolve().parents[4]


def _release() -> tuple[StrategyRelease, dict[str, object]]:
    raw = json.loads((ROOT / "strategies/S002/versions/v1.json").read_text(encoding="utf-8"))
    release = StrategyRelease.from_mapping(raw)
    return release, raw


def test_loader_discovers_s002_without_a_central_release_switch() -> None:
    release, _ = _release()

    strategy = StrategyLoader(ROOT / "strategies").load(release)

    assert strategy.definition.release_id == "S002-v1"
    assert strategy.definition.implementation.module.endswith("strategies.s002_v1")
    assert strategy.definition.runtime_sha256


def test_loader_fails_clearly_when_implementation_is_missing() -> None:
    raw = {
        "strategy_id": "S999",
        "version": "v1",
        "release_id": "S999-v1",
        "strategy_payload": {"kind": "test"},
    }
    from strategy_runtime import canonical_sha256

    raw["release_hash"] = canonical_sha256(raw)
    release = StrategyRelease.from_mapping(raw)

    with pytest.raises(RuntimeCompatibilityError, match="deployment file"):
        StrategyLoader(ROOT / "strategies").load(release)


def test_strategy_release_rejects_payload_with_a_borrowed_hash() -> None:
    _, raw = _release()
    raw["strategy_payload"]["rule"]["portfolio_rule"]["holding_sessions"] = 6

    with pytest.raises(RuntimeContractError, match="complete frozen record"):
        StrategyRelease.from_mapping(raw)
