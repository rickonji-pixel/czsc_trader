from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from dataflows import Dataflows, Dataset
from strategy_runtime import (
    ExecutionState,
    PortfolioSnapshot,
    RuntimeContractError,
    StrategyInit,
    StrategyRelease,
    StrategyRuntime,
    TradableWindow,
    TradingPoint,
)


def test_legacy_runner_surface_is_not_public() -> None:
    import strategy_runtime

    for name in (
        "AccountSnapshot",
        "CalculationRequest",
        "DeploymentSpec",
        "ExecutionChannel",
        "StrategyDecision",
        "StrategyLoader",
        "StrategyRunner",
        "StrategyRuntimeContext",
        "StrategyDataSource",
        "PublishedDataSource",
        "HistoricalDataSource",
        "PreparedStrategyData",
        "PublishedStrategyData",
        "PublicationStatus",
        "publish_history",
        "read_publication",
        "write_publication",
        "validate_publication",
        "load_strategy_runtime_context",
    ):
        assert not hasattr(strategy_runtime, name)


ROOT = Path(__file__).resolve().parents[4]
ZONE = ZoneInfo("Asia/Shanghai")


def _release(strategy_id: str, version: str) -> StrategyRelease:
    payload = json.loads(
        (ROOT / f"strategies/{strategy_id}/versions/{version}.json").read_text(
            encoding="utf-8"
        )
    )
    return StrategyRelease.from_mapping(payload)


def _flows() -> Dataflows:
    dates = pd.bdate_range(end="2026-09-02", periods=700)
    bars = pd.DataFrame(
        {
            "Date": dates,
            "Open": 6.0,
            "High": 6.1,
            "Low": 5.9,
            "Close": 6.0,
            "Volume": 1000.0,
            "Amount": 6000.0,
        }
    )

    def market(request):
        frame = bars.loc[
            pd.to_datetime(bars["Date"]).between(request.start, request.end)
        ].copy()
        return frame, {
            "vendor": "test",
            "adjustment": "none" if "unadjusted" in request.dataset else "hfq",
            "primary_key": ["Date"],
        }

    def calendar(request):
        days = pd.date_range(request.start, request.end)
        return pd.DataFrame(
            {"Date": days, "IsOpen": (days.dayofweek < 5).astype(int)}
        ), {"vendor": "test", "primary_key": ["Date"]}

    return Dataflows(
        {
            Dataset.ETF_OHLCV.value: market,
            Dataset.ETF_UNADJUSTED_DAILY.value: market,
            Dataset.TRADING_CALENDAR.value: calendar,
        }
    )


def test_public_runtime_prepares_and_plans_without_an_execution_channel(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("strategy_runtime.preparation.Dataflows", lambda: _flows())
    trading_date = date(2026, 9, 3)
    strategy = StrategyRuntime().create(
        StrategyInit(
            _release("S002", "v1"),
            TradableWindow(trading_date, trading_date),
            tmp_path,
        )
    )
    calculated_at = datetime(2026, 9, 2, 22, 14, tzinfo=ZONE)
    with pytest.raises(RuntimeContractError, match="call prepare_data"):
        strategy.plan_at(
            point=TradingPoint(trading_date, calculated_at),
            portfolio=PortfolioSnapshot(
                "s002-v1",
                "510500.SH",
                Decimal("50000"),
                Decimal("100000"),
                5900,
                7,
                calculated_at,
            ),
            state=ExecutionState(3, calculated_at, 5900),
        )

    prepared = strategy.prepare_data()
    plan = strategy.plan_at(
        point=TradingPoint(trading_date, calculated_at),
        portfolio=PortfolioSnapshot(
            "s002-v1",
            "510500.SH",
            Decimal("50000"),
            Decimal("100000"),
            5900,
            7,
            calculated_at,
        ),
        state=ExecutionState(3, calculated_at, 5900),
    )

    assert prepared.strategy.reference_id == "S002-v1"
    assert prepared.available_through == date(2026, 9, 2)
    assert (tmp_path / "prepared-data.json").is_file()
    assert plan.strategy.reference_id == "S002-v1"
    assert plan.expected_portfolio_revision == 7
    assert plan.expected_state_revision == 3
    assert plan.actual_quantity == 5900
    assert plan.signal_identity != plan.plan_identity
    assert plan.trading_date == trading_date
    assert {order.order_type.value for order in plan.orders} <= {"LIMIT", "MARKET"}
    assert {leg.order.order_type.value for leg in plan.legs} <= {"LIMIT", "MARKET"}

    monkeypatch.setattr(
        "strategy_runtime.preparation.Dataflows",
        lambda: (_ for _ in ()).throw(AssertionError("cache must be self-contained")),
    )
    cached = StrategyRuntime().create(
        StrategyInit(
            _release("S002", "v1"),
            TradableWindow(trading_date, trading_date),
            tmp_path,
        )
    ).prepare_data()
    assert cached == prepared

    with pytest.raises(RuntimeContractError, match="another strategy instance"):
        StrategyRuntime().create(
            StrategyInit(
                _release("S002", "v1"),
                TradableWindow(date(2026, 9, 2), date(2026, 9, 2)),
                tmp_path,
            )
        ).prepare_data()

    manifest = json.loads((tmp_path / "prepared-data.json").read_text(encoding="utf-8"))
    input_file = tmp_path / next(iter(manifest["inputs"].values()))["file"]
    input_file.write_bytes(input_file.read_bytes() + b"changed")
    with pytest.raises(RuntimeContractError, match="file was modified"):
        StrategyRuntime().create(
            StrategyInit(
                _release("S002", "v1"),
                TradableWindow(trading_date, trading_date),
                tmp_path,
            )
        ).prepare_data()


def test_failed_preparation_is_not_exposed_as_prepared_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "strategy_runtime.preparation.Dataflows",
        lambda: (_ for _ in ()).throw(RuntimeError("DFLS unavailable")),
    )
    strategy = StrategyRuntime().create(
        StrategyInit(
            _release("S002", "v1"),
            TradableWindow(date(2026, 9, 3), date(2026, 9, 3)),
            tmp_path,
        )
    )
    with pytest.raises(RuntimeError, match="DFLS unavailable"):
        strategy.prepare_data()
    assert not (tmp_path / "prepared-data.json").exists()
    with pytest.raises(RuntimeContractError, match="call prepare_data"):
        strategy.inspect_signals()
