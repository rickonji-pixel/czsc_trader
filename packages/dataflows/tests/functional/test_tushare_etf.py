from __future__ import annotations

import pandas as pd

from dataflows import tushare_etf


def test_etf_long_history_fetch_segments_adjustment_factors(monkeypatch) -> None:
    class FakePro:
        def __init__(self) -> None:
            self.factor_requests: list[tuple[str, str]] = []

        def fund_daily(self, **_kwargs):
            return pd.DataFrame([
                {"trade_date": "20130315", "open": 1, "high": 1, "low": 1,
                 "close": 1, "vol": 1, "amount": 1},
                {"trade_date": "20260908", "open": 2, "high": 2, "low": 2,
                 "close": 2, "vol": 1, "amount": 1},
            ])

        def fund_adj(self, *, start_date, end_date, **_kwargs):
            self.factor_requests.append((start_date, end_date))
            factors = pd.DataFrame([
                {"trade_date": "20130315", "adj_factor": 1.0},
                {"trade_date": "20200102", "adj_factor": 1.5},
                {"trade_date": "20260908", "adj_factor": 2.0},
            ])
            return factors.loc[
                factors["trade_date"].between(start_date, end_date)
            ].reset_index(drop=True)

    pro = FakePro()
    monkeypatch.setattr(tushare_etf, "get_tushare_pro", lambda _env=None: pro)

    bars, metadata = tushare_etf.fetch_etf_ohlcv(
        "510500.SH", "2013-03-15", "2026-09-08", "daily"
    )

    assert pro.factor_requests == [
        ("20130315", "20171231"),
        ("20180101", "20221231"),
        ("20230101", "20260908"),
    ]
    assert bars["Close"].tolist() == [1.0, 4.0]
    assert metadata["adjustment_factor_source"] == "fund_adj"
