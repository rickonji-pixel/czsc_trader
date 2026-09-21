from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dataflows import Dataflows, Dataset

from paper_trading_engine.srt_advice_client import SrtAdviceClient


ROOT = Path(__file__).resolve().parents[4]


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
        }

    def calendar(request):
        days = pd.date_range(request.start, request.end)
        return pd.DataFrame(
            {"Date": days, "IsOpen": (days.dayofweek < 5).astype(int)}
        ), {"vendor": "test"}

    return Dataflows(
        {
            Dataset.ETF_OHLCV.value: market,
            Dataset.ETF_UNADJUSTED_DAILY.value: market,
            Dataset.TRADING_CALENDAR.value: calendar,
        }
    )


def _client(tmp_path, account_sessions):
    return SrtAdviceClient(
        repo_root=ROOT,
        data_dir=tmp_path,
        now=lambda: datetime(2026, 9, 2, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        session_resolver=lambda signal_date: account_sessions.get(signal_date),
    )


def test_pte_prepares_then_uses_one_account_strategy_instance(tmp_path, monkeypatch):
    monkeypatch.setattr("strategy_runtime.preparation.Dataflows", lambda: _flows())
    client = _client(tmp_path, {date(2026, 9, 2): date(2026, 9, 3)})
    prepared = client.prepare_account_data(
        account_id="s002-v1",
        strategy_id="S002",
        strategy_version="v1",
        symbol="510500.SH",
        asset="etf",
        signal_date=date(2026, 9, 2),
    )
    assert prepared is not None

    monkeypatch.setattr(
        "strategy_runtime.preparation.Dataflows",
        lambda: (_ for _ in ()).throw(AssertionError("decision must use prepared data")),
    )
    decision = client.get_decision(
        0,
        100_000,
        100_000,
        trading_date=date(2026, 9, 3),
        portfolio_revision=0,
        state_revision=0,
        strategy_id="S002",
        strategy_version="v1",
        account_id="s002-v1",
        symbol="510500.SH",
        asset="etf",
    )
    assert client.prepared_through("s002-v1", "S002", "v1") == date(2026, 9, 2)
    assert client.tradable_date("s002-v1", "S002", "v1") == date(2026, 9, 3)
    assert decision.signal_date == date(2026, 9, 2)
    assert decision.valid_session == date(2026, 9, 3)


def test_prepared_data_is_isolated_by_account(tmp_path, monkeypatch):
    monkeypatch.setattr("strategy_runtime.preparation.Dataflows", lambda: _flows())
    client = _client(tmp_path, {date(2026, 9, 2): date(2026, 9, 3)})
    for account_id, strategy_id, symbol in (
        ("s002-v1", "S002", "510500.SH"),
        ("s002-v1-alt", "S002", "510500.SH"),
    ):
        prepared = client.prepare_account_data(
            account_id=account_id,
            strategy_id=strategy_id,
            strategy_version="v1",
            symbol=symbol,
            asset="etf",
            signal_date=date(2026, 9, 2),
        )
        assert prepared is not None
    assert (tmp_path / "accounts/s002-v1/current.json").is_file()
    assert (tmp_path / "accounts/s002-v1-alt/current.json").is_file()
    assert client.tradable_date("s002-v1", "S002", "v1") == date(2026, 9, 3)
    assert client.tradable_date("s002-v1-alt", "S002", "v1") == date(2026, 9, 3)


def test_closed_day_does_not_prepare_or_publish_account_data(tmp_path):
    client = _client(tmp_path, {date(2026, 9, 5): None})
    result = client.prepare_account_data(
        account_id="s002-v1",
        strategy_id="S002",
        strategy_version="v1",
        symbol="510500.SH",
        asset="etf",
        signal_date=date(2026, 9, 5),
    )
    assert result is None
    assert not (tmp_path / "accounts/s002-v1/current.json").exists()


def test_default_session_resolver_uses_sse_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "paper_trading_engine.srt_advice_client.Dataflows", lambda: _flows()
    )
    client = SrtAdviceClient(repo_root=ROOT, data_dir=tmp_path)
    assert client._next_tradable_session(date(2026, 9, 2)) == date(2026, 9, 3)
    assert client._next_tradable_session(date(2026, 9, 5)) is None
