from __future__ import annotations

import pandas as pd

from .formatting import format_dataframe_report, unsupported_response
from .market_resolver import detect_market, normalize_symbol_for_vendor
from .tushare_common import get_tushare_pro


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    try:
        market = detect_market(ticker)
        symbol = normalize_symbol_for_vendor(ticker, "tushare", market)
        pro = get_tushare_pro()
        dataframe = pro.news(
            start_date=start_date.replace("-", ""), end_date=end_date.replace("-", "")
        )
        if dataframe is not None and not dataframe.empty:
            filters = []
            for column in ["title", "content", "name", "ts_code"]:
                if column in dataframe.columns:
                    filters.append(
                        dataframe[column].astype(str).str.contains(ticker, case=False, na=False)
                    )
                    filters.append(
                        dataframe[column].astype(str).str.contains(symbol, case=False, na=False)
                    )
            if filters:
                mask = filters[0]
                for item in filters[1:]:
                    mask = mask | item
                filtered = dataframe[mask]
                if not filtered.empty:
                    dataframe = filtered
        return format_dataframe_report(
            f"Tushare news for {ticker}",
            dataframe,
            {"Vendor": "tushare", "Market": market, "Start date": start_date, "End date": end_date},
        )
    except Exception as exc:
        return f"Error retrieving news for {ticker} via tushare: {exc}"


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 50) -> str:
    try:
        pro = get_tushare_pro()
        dataframe = pro.cctv_news(date=curr_date.replace("-", ""))
        if isinstance(dataframe, pd.DataFrame) and not dataframe.empty:
            dataframe = dataframe.head(limit)
        return format_dataframe_report(
            "Tushare global news",
            dataframe,
            {
                "Vendor": "tushare",
                "Date": curr_date,
                "Look back days": look_back_days,
                "Limit": limit,
            },
        )
    except Exception as exc:
        return f"Error retrieving global news via tushare: {exc}"


def get_insider_transactions(ticker: str) -> str:
    market = detect_market(ticker)
    return unsupported_response(
        "tushare",
        "get_insider_transactions",
        market,
        "This endpoint was not validated and may be permission-gated.",
    )
