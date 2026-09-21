from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy_runtime import StrategyRelease
from strategy_runtime.loader import StrategyLoader


ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_s001_frozen_versions_load_their_bound_runtime(version: str) -> None:
    raw = json.loads(
        (ROOT / f"strategies/S001/versions/{version}.json").read_text(encoding="utf-8")
    )
    release = StrategyRelease.from_mapping(raw)
    strategy = StrategyLoader().load(release)

    assert strategy.definition.release_id == f"S001-{version}"
    assert strategy.definition.runtime_sha256
