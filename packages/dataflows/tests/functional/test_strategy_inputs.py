from __future__ import annotations

import pandas as pd
import pytest

from dataflows.errors import DataContractError
from dataflows.tushare_strategy_data import (
    fetch_etf_share_size,
    fetch_global_index_daily,
    fetch_index_constituent_weight,
    fetch_index_daily_basic,
    fetch_shibor_daily,
    fetch_stock_moneyflow,
    fetch_stock_moneyflow_sessions,
    fetch_trading_calendar,
)


class FakePro:
    def shibor(self, **kwargs):
        return pd.DataFrame({"date": ["20260915"], "on": [1.25]})

    def index_dailybasic(self, **kwargs):
        return pd.DataFrame({"trade_date": ["20260915"], "turnover_rate_f": [2.5]})

    def etf_share_size(self, **kwargs):
        return pd.DataFrame({"trade_date": ["20260915"], "total_share": [123.0]})

    def index_global(self, **kwargs):
        return pd.DataFrame({"trade_date": ["20260915"], "pct_chg": [1.5]})

    def index_weight(self, **kwargs):
        return pd.DataFrame(
            {
                "trade_date": ["20260915", "20260915"],
                "con_code": ["000001.SZ", "600000.SH"],
                "weight": [40.0, 60.0],
            }
        )

    def moneyflow(self, **kwargs):
        trade_date = kwargs.get("trade_date", "20260915")
        return pd.DataFrame(
            {
                "trade_date": [trade_date, trade_date],
                "ts_code": ["000001.SZ", "600000.SH"],
                "net_mf_amount": [10.0, -5.0],
            }
        )

    def trade_cal(self, **kwargs):
        return pd.DataFrame(
            {
                "cal_date": ["20260915", "20260916"],
                "is_open": [1, 1],
                "pretrade_date": ["20260914", "20260915"],
            }
        )


class ChunkedShiborPro:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def shibor(self, **kwargs):
        self.calls.append(kwargs)
        return pd.DataFrame({"date": [kwargs["start_date"]], "on": [1.25]})


def test_s007_non_ohlcv_inputs_are_canonical() -> None:
    pro = FakePro()
    shibor, _ = fetch_shibor_daily("2026-09-15", "2026-09-15", pro=pro)
    chinext, _ = fetch_index_daily_basic("399006.SZ", "2026-09-15", "2026-09-15", pro=pro)
    shares, share_meta = fetch_etf_share_size("588080.SH", "2026-09-15", "2026-09-15", pro=pro)
    spx, _ = fetch_global_index_daily("SPX", "2026-09-15", "2026-09-15", pro=pro)

    assert shibor.loc[0, "OvernightRate"] == pytest.approx(1.25)
    assert chinext.loc[0, "TurnoverRateFreeFloat"] == pytest.approx(2.5)
    assert shares.loc[0, "TotalShare"] == pytest.approx(123.0)
    assert share_meta["availability_rule"] == "T+1 08:30 Asia/Shanghai"
    assert spx.loc[0, "PercentChange"] == pytest.approx(0.015)


def test_shibor_long_history_is_split_by_calendar_year() -> None:
    pro = ChunkedShiborPro()

    frame, metadata = fetch_shibor_daily("2024-12-31", "2026-01-01", pro=pro)

    assert frame["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2024-12-31",
        "2025-01-01",
        "2026-01-01",
    ]
    assert [call["start_date"] for call in pro.calls] == [
        "20241231",
        "20250101",
        "20260101",
    ]
    assert metadata["maximum_start_lag_days"] == 10


def test_s003_constituent_inputs_preserve_multi_entity_keys() -> None:
    pro = FakePro()
    weights, weight_meta = fetch_index_constituent_weight(
        "000905.SH", "2026-09-15", "2026-09-15", pro=pro
    )
    flows, flow_meta = fetch_stock_moneyflow("2026-09-15", "2026-09-15", pro=pro)

    assert list(weights.columns) == ["Date", "ConstituentSymbol", "Weight"]
    assert weight_meta["primary_key"] == ["Date", "ConstituentSymbol"]
    assert list(flows.columns) == ["Date", "Symbol", "NetMoneyflowAmount"]
    assert flow_meta["primary_key"] == ["Date", "Symbol"]
    assert len(weights) == len(flows) == 2


def test_all_market_moneyflow_rejects_unsafe_multi_day_request() -> None:
    with pytest.raises(DataContractError, match="exactly one day"):
        fetch_stock_moneyflow("2026-09-14", "2026-09-15", pro=FakePro())


def test_all_market_moneyflow_can_publish_explicit_sessions() -> None:
    flows, metadata = fetch_stock_moneyflow_sessions(("2026-09-15", "2026-09-16"), pro=FakePro())

    assert len(flows) == 4
    assert metadata["requested_trading_dates"] == ["2026-09-15", "2026-09-16"]
    assert metadata["primary_key"] == ["Date", "Symbol"]


def test_trading_calendar_is_canonical() -> None:
    calendar, metadata = fetch_trading_calendar("SSE", "2026-09-15", "2026-09-16", pro=FakePro())

    assert list(calendar.columns) == ["Date", "IsOpen", "PreviousTradingDate"]
    assert calendar["IsOpen"].tolist() == [1, 1]
    assert metadata["exchange"] == "SSE"
