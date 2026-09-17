from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.baselines import resolve_strategy_payload
from czsc_trader.data import load_market_data
from czsc_trader.strategy_runtime import apply_resolved_strategy
from strategy_runtime import StrategyLoader, StrategyRelease
from strategy_runtime.strategies.s001_common import calculate_s001_history


ROOT = Path(__file__).resolve().parents[4]


def _published(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(
        columns={
            "dt": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "vol": "Volume",
            "amount": "Amount",
        }
    ).drop(columns=["symbol"])


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_s001_complete_target_history_matches_frozen_legacy_path(version: str) -> None:
    raw = json.loads(
        (ROOT / f"strategies/S001/versions/{version}.json").read_text(encoding="utf-8")
    )
    release = StrategyRelease.from_mapping(raw)
    strategy = StrategyLoader().load(release)
    data = load_market_data(ROOT / "data/backtest", "588080.SH", "etf")

    actual = calculate_s001_history(
        _published(data.intraday),
        _published(data.daily),
        _published(data.weekly),
        raw["strategy_payload"]["rule"],
        "588080.SH",
    )
    legacy = resolve_strategy_payload(
        ROOT / "strategies/dependencies/legacy_rule_baselines",
        raw["strategy_payload"],
        release_id=release.release_id,
        release_hash=release.release_hash,
        symbol="588080.SH",
        repository_root=ROOT,
    )
    expected = apply_resolved_strategy(data, legacy)

    pd.testing.assert_index_equal(actual.index, expected.target_position.index)
    pd.testing.assert_series_equal(
        actual["target_position"], expected.target_position, check_names=False
    )
    pd.testing.assert_series_equal(actual["factor_score"], expected.scores, check_names=False)
    pd.testing.assert_series_equal(
        actual["regime"].astype("string"), expected.regimes.astype("string"), check_names=False
    )
    assert strategy.definition.release_id == f"S001-{version}"
