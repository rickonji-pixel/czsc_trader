from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from dataflows import Dataflows, Dataset

from strategy_runtime import (
    ExecutionState,
    PortfolioSnapshot,
    StrategyImplementation,
    StrategyInit,
    StrategyRelease,
    StrategyRuntime,
    TradableWindow,
    TradingPoint,
    canonical_sha256,
)
from strategy_runtime.loader import StrategyLoader
from strategy_runtime.strategies import s001_common


ROOT = Path(__file__).resolve().parents[4]
BASELINE_PATH = ROOT / "packages/strategy_runtime/tests/fixtures/s001_equivalence.json"
ZONE = ZoneInfo("Asia/Shanghai")


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _market_frame(kind: str, date_column: str) -> pd.DataFrame:
    frames = []
    for path in sorted((ROOT / "data/raw").glob(f"588080_{kind}_*.csv")):
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
                frame["Date"].between("2020-11-16", "2026-06-30")
            ]
        )
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values("Date")
        .drop_duplicates("Date", keep="last")
        .reset_index(drop=True)
    )


def _calculation_inputs() -> dict[str, pd.DataFrame]:
    return {
        "adjusted_30m": _market_frame("30m", "datetime"),
        "adjusted_daily": _market_frame("daily", "date"),
        "adjusted_weekly": _market_frame("weekly", "date"),
    }


def _execution_input() -> pd.DataFrame:
    return _market_frame("execution_daily", "date")


def _flows(requests: list[object]) -> Dataflows:
    inputs = _calculation_inputs()
    execution = _execution_input()
    open_dates = set(pd.to_datetime(inputs["adjusted_daily"]["Date"]).dt.normalize())

    def market(request):
        requests.append(request)
        if request.dataset == Dataset.ETF_UNADJUSTED_DAILY.value:
            source = execution
            adjustment = "none"
        else:
            source = {
                "30m": inputs["adjusted_30m"],
                "daily": inputs["adjusted_daily"],
                "weekly": inputs["adjusted_weekly"],
            }[request.frequency]
            adjustment = "hfq"
        dates = pd.to_datetime(source["Date"])
        request_end = pd.Timestamp(request.end)
        if len(str(request.end)) == 10:
            request_end += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        frame = source.loc[
            dates.between(pd.Timestamp(request.start), request_end)
        ].copy()
        return frame, {
            "vendor": "s001-equivalence",
            "adjustment": adjustment,
            "primary_key": ["Date"],
        }

    def calendar(request):
        requests.append(request)
        days = pd.date_range(request.start, request.end)
        return pd.DataFrame(
            {"Date": days, "IsOpen": days.normalize().isin(open_dates).astype(int)}
        ), {"vendor": "s001-equivalence", "primary_key": ["Date"]}

    return Dataflows(
        {
            Dataset.ETF_OHLCV.value: market,
            Dataset.ETF_UNADJUSTED_DAILY.value: market,
            Dataset.TRADING_CALENDAR.value: calendar,
        }
    )


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

    assert calendar_window.start == date(2020, 11, 16)
    assert calendar_window.end == window.end + timedelta(days=20)
    assert scope.trading_dates == tuple(pd.bdate_range(window.start, window.end).date)
    assert scope.signal_dates == {
        date(2026, 1, 5): date(2026, 1, 2),
        date(2026, 1, 6): date(2026, 1, 5),
        date(2026, 1, 7): date(2026, 1, 6),
        date(2026, 1, 8): date(2026, 1, 7),
        date(2026, 1, 9): date(2026, 1, 8),
    }
    assert scope.calculation_dates[0] == date(2020, 11, 16)
    assert scope.calculation_dates[-1] == date(2026, 1, 8)
    assert {
        name: (item.start, item.end, item.required_cutoff)
        for name, item in scope.inputs.items()
    } == {
        "adjusted_30m": (date(2020, 11, 16), date(2026, 1, 8), date(2026, 1, 8)),
        "adjusted_daily": (date(2020, 11, 16), date(2026, 1, 8), date(2026, 1, 8)),
        "adjusted_weekly": (date(2020, 11, 16), date(2026, 1, 8), None),
        "execution_daily": (date(2020, 11, 16), date(2026, 1, 8), date(2026, 1, 8)),
        "trading_calendar": (date(2020, 11, 16), date(2026, 1, 29), date(2026, 1, 29)),
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


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_s001_single_session_preparation_reproduces_the_canonical_signal_and_plan(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.loads(
        (ROOT / f"strategies/S001/versions/{version}.json").read_text(encoding="utf-8")
    )
    release = StrategyRelease.from_mapping(raw)
    requests: list[object] = []
    monkeypatch.setattr(
        "strategy_runtime.preparation.Dataflows",
        lambda: _flows(requests),
    )
    trading_date = date(2026, 1, 30)
    instance = StrategyRuntime().create(
        StrategyInit(
            release,
            TradableWindow(trading_date, trading_date),
            tmp_path / version,
        )
    )

    prepared = instance.prepare_data()
    history = instance.inspect_signals()
    full_inputs = _calculation_inputs()
    canonical = s001_common.calculate_s001_history(
        full_inputs["adjusted_30m"],
        full_inputs["adjusted_daily"],
        full_inputs["adjusted_weekly"],
        raw["strategy_payload"]["rule"],
        "588080.SH",
    )
    signal_date = pd.Timestamp("2026-01-29")
    calculated_at = datetime(2026, 1, 29, 20, 31, tzinfo=ZONE)
    plan = instance.plan_at(
        point=TradingPoint(trading_date, calculated_at),
        portfolio=PortfolioSnapshot(
            "s001-equivalence",
            "588080.SH",
            Decimal("0"),
            Decimal("1000"),
            1000,
            0,
            calculated_at,
        ),
        state=ExecutionState(0, calculated_at, 1000),
    )

    assert prepared.available_through == signal_date.date()
    assert {request.start for request in requests} == {"2020-11-16"}
    pd.testing.assert_series_equal(
        history.loc[signal_date],
        canonical.loc[signal_date],
        check_names=False,
    )
    assert plan.signal_date == signal_date.date()
    assert plan.target_position == 0.0
    assert plan.action == "SELL"
    assert plan.actual_quantity == 1000
    assert plan.target_quantity == 0
    assert len(plan.orders) == 1
    assert plan.orders[0].side.value == "SELL"
    assert plan.orders[0].quantity == 1000
    assert plan.orders[0].order_type.value == "MARKET"


@pytest.mark.parametrize(
    ("version", "signal_date", "trading_date"),
    [
        ("v1", date(2025, 12, 18), date(2025, 12, 19)),
        ("v2", date(2026, 1, 13), date(2026, 1, 14)),
    ],
)
def test_s001_point_and_window_calculations_apply_their_distinct_state_boundaries(
    version: str,
    signal_date: date,
    trading_date: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.loads(
        (ROOT / f"strategies/S001/versions/{version}.json").read_text(encoding="utf-8")
    )
    requests: list[object] = []
    monkeypatch.setattr(
        "strategy_runtime.preparation.Dataflows",
        lambda: _flows(requests),
    )
    instance = StrategyRuntime().create(
        StrategyInit(
            StrategyRelease.from_mapping(raw),
            TradableWindow(trading_date, trading_date),
            tmp_path / version,
        )
    )
    instance.prepare_data()
    window_history = instance.inspect_signals()
    calculated_at = datetime.combine(
        signal_date,
        datetime.min.time().replace(hour=20, minute=31),
        tzinfo=ZONE,
    )
    plan = instance.plan_at(
        point=TradingPoint(trading_date, calculated_at),
        portfolio=PortfolioSnapshot(
            "s001-equivalence",
            "588080.SH",
            Decimal("0"),
            Decimal("1000"),
            1000,
            0,
            calculated_at,
        ),
        state=ExecutionState(0, calculated_at, 1000),
    )

    assert window_history.loc[pd.Timestamp(signal_date), "target_position"] == 0.0
    assert plan.target_position == 1.0
    assert plan.action == "HOLD"
    assert plan.actual_quantity == 1000
    assert plan.target_quantity == 1000
    assert not plan.orders
