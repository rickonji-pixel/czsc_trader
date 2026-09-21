from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dataflows import Dataflows, Dataset

from paper_trading_engine.srt_advice_client import SrtAdviceClient
from strategy_runtime.prepare_cli import prepare_runtime_data


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


def test_pte_uses_only_the_prepared_index_and_strategy_instance(tmp_path, monkeypatch):
    monkeypatch.setattr("strategy_runtime.preparation.Dataflows", lambda: _flows())
    index = prepare_runtime_data(
        repo_root=ROOT,
        data_dir=tmp_path,
        symbol="510500.SH",
        releases=[("S002", "v1")],
        trading_date=date(2026, 9, 3),
    )
    assert index["signal_date"] == "2026-09-02"
    assert index["trading_date"] == "2026-09-03"

    monkeypatch.setattr(
        "strategy_runtime.preparation.Dataflows",
        lambda: (_ for _ in ()).throw(AssertionError("PTE must use prepared data")),
    )
    client = SrtAdviceClient(
        repo_root=ROOT,
        data_dir=tmp_path,
        now=lambda: datetime(2026, 9, 2, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    prepared = client.prepare_for_account(
        strategy_id="S002",
        strategy_version="v1",
        symbol="510500.SH",
        asset="etf",
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
    assert prepared.data_identity == index["releases"]["S002-v1"]["data_identity"]
    assert decision.signal_date == date(2026, 9, 2)
    assert decision.valid_session == date(2026, 9, 3)
