from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

import pandas as pd
import pytest

from strategy_runtime import (
    StrategyImplementation,
    StrategyRelease,
    TradableWindow,
    canonical_sha256,
)
from strategy_runtime.loader import StrategyLoader
from strategy_runtime.strategies import s001_common


ROOT = Path(__file__).resolve().parents[4]
BASELINE_PATH = ROOT / "packages/strategy_runtime/tests/fixtures/s001_equivalence.json"


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _market_frame(kind: str, date_column: str) -> pd.DataFrame:
    frames = []
    for path in sorted((ROOT / "data/backtest").glob(f"588080_{kind}_*.csv")):
        year = int(path.stem.rsplit("_", 1)[-1])
        if year not in {2024, 2025, 2026}:
            continue
        frame = pd.read_csv(path).rename(
            columns={
                date_column: "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
                "amount": "Amount",
            }
        )
        frame["Date"] = pd.to_datetime(frame["Date"])
        frames.append(
            frame.loc[
                frame["Date"].between("2024-01-02", "2026-06-30")
            ]
        )
    return pd.concat(frames, ignore_index=True).sort_values("Date").reset_index(drop=True)


def _calculation_inputs() -> dict[str, pd.DataFrame]:
    return {
        "adjusted_30m": _market_frame("30m", "datetime"),
        "adjusted_daily": _market_frame("daily", "date"),
        "adjusted_weekly": _market_frame("weekly", "date"),
    }


def _history_projection(history: pd.DataFrame) -> list[dict[str, object]]:
    return [
        {
            "date": pd.Timestamp(index).isoformat(),
            "factor_score": round(float(row.factor_score), 12),
            "regime": str(row.regime),
            "target_position": round(float(row.target_position), 12),
        }
        for index, row in history.iterrows()
    ]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_s001_frozen_versions_load_their_bound_runtime(version: str) -> None:
    raw = json.loads(
        (ROOT / f"strategies/S001/versions/{version}.json").read_text(encoding="utf-8")
    )
    release = StrategyRelease.from_mapping(raw)
    strategy = StrategyLoader().load(release)

    assert isinstance(strategy, StrategyImplementation)
    assert strategy.definition.release_id == f"S001-{version}"
    assert strategy.definition.runtime_sha256


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_s001_frozen_versions_derive_their_calculation_scope(version: str) -> None:
    raw = json.loads(
        (ROOT / f"strategies/S001/versions/{version}.json").read_text(encoding="utf-8")
    )
    strategy = StrategyLoader().load(StrategyRelease.from_mapping(raw))
    window = TradableWindow(date(2026, 1, 5), date(2026, 1, 9))

    calendar_window = strategy.calendar_window(window)
    calendar_dates = tuple(pd.bdate_range(calendar_window.start, calendar_window.end).date)
    scope = strategy.derive_calculation_scope(window, calendar_dates)

    assert calendar_window.start == window.start - timedelta(days=1000)
    assert calendar_window.end == window.end + timedelta(days=20)
    assert scope.trading_dates == tuple(pd.bdate_range(window.start, window.end).date)
    assert scope.signal_dates == {
        date(2026, 1, 5): date(2026, 1, 2),
        date(2026, 1, 6): date(2026, 1, 5),
        date(2026, 1, 7): date(2026, 1, 6),
        date(2026, 1, 8): date(2026, 1, 7),
        date(2026, 1, 9): date(2026, 1, 8),
    }
    assert scope.calculation_dates == tuple(
        pd.bdate_range("2026-01-02", "2026-01-08").date
    )
    assert {
        name: (item.start, item.end, item.required_cutoff)
        for name, item in scope.inputs.items()
    } == {
        "adjusted_30m": (date(2024, 2, 5), date(2026, 1, 8), date(2026, 1, 8)),
        "adjusted_daily": (date(2025, 10, 13), date(2026, 1, 8), date(2026, 1, 8)),
        "adjusted_weekly": (date(2025, 12, 15), date(2026, 1, 8), None),
        "execution_daily": (date(2026, 1, 2), date(2026, 1, 8), date(2026, 1, 8)),
        "trading_calendar": (date(2023, 4, 11), date(2026, 1, 29), date(2026, 1, 29)),
    }


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_s001_refactor_preserves_frozen_signal_history(version: str) -> None:
    raw = json.loads(
        (ROOT / f"strategies/S001/versions/{version}.json").read_text(encoding="utf-8")
    )
    strategy = StrategyLoader().load(StrategyRelease.from_mapping(raw))
    inputs = _calculation_inputs()
    sessions = pd.DatetimeIndex(inputs["adjusted_daily"]["Date"])
    expected = s001_common.calculate_s001_history(
        inputs["adjusted_30m"],
        inputs["adjusted_daily"],
        inputs["adjusted_weekly"],
        raw["strategy_payload"]["rule"],
        "588080.SH",
    )
    actual = strategy.calculate_history(inputs, sessions)
    baseline = _baseline()[version]

    pd.testing.assert_frame_equal(actual, expected)
    assert {name: len(frame) for name, frame in inputs.items()} == baseline["input_rows"]
    assert len(actual) == baseline["history_rows"]
    assert actual.index.min().date().isoformat() == baseline["history_start"]
    assert actual.index.max().date().isoformat() == baseline["history_end"]
    assert actual["regime"].value_counts().sort_index().to_dict() == baseline["regimes"]
    assert {
        str(target): int(count)
        for target, count in actual["target_position"].value_counts().sort_index().items()
    } == baseline["target_positions"]
    assert int(actual["target_position"].diff().eq(1.0).sum()) == baseline["entries"]
    assert int(actual["target_position"].diff().eq(-1.0).sum()) == baseline["exits"]
    assert canonical_sha256(_history_projection(actual)) == baseline["history_sha256"]
