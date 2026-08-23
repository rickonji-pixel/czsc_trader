from __future__ import annotations

import pandas as pd
from typing import cast

from .formatting import format_dataframe_report, format_text_report, unsupported_response
from .market_resolver import (
    MARKET_A_SHARE,
    MARKET_HK,
    detect_market,
    normalize_symbol_for_vendor,
)
from .tushare_common import get_tushare_pro


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    try:
        market = detect_market(ticker)
        symbol = normalize_symbol_for_vendor(ticker, "tushare", market)
        pro = get_tushare_pro()
        if market == MARKET_A_SHARE:
            dataframe = pro.stock_basic(ts_code=symbol)
        elif market == MARKET_HK:
            dataframe = pro.hk_basic(ts_code=symbol)
        else:
            dataframe = pro.us_basic(ts_code=symbol)
        if dataframe is None or dataframe.empty:
            return f"No fundamentals data found for {ticker} via tushare."
        row = dataframe.iloc[0].dropna()
        lines = [f"{column}: {value}" for column, value in row.items()]
        return format_text_report(
            f"Tushare fundamentals for {ticker}",
            lines,
            {"Vendor": "tushare", "Market": market, "Vendor symbol": symbol},
        )
    except Exception as exc:
        return f"Error retrieving fundamentals for {ticker} via tushare: {exc}"


def _fetch_statement(
    ticker: str, method_name: str, curr_date: str | None = None
) -> tuple[pd.DataFrame, str, str]:
    market = detect_market(ticker)
    if market != MARKET_A_SHARE:
        raise ValueError(
            unsupported_response(
                "tushare",
                method_name,
                market,
                "Only A-share financial statements are wired in this implementation.",
            )
        )
    symbol = normalize_symbol_for_vendor(ticker, "tushare", market)
    pro = get_tushare_pro()
    dataframe = getattr(pro, method_name)(ts_code=symbol)
    if not isinstance(dataframe, pd.DataFrame):
        dataframe = pd.DataFrame(dataframe)
    dataframe = cast(pd.DataFrame, dataframe)
    if curr_date and dataframe is not None and not dataframe.empty:
        for column in ["end_date", "f_ann_date", "ann_date"]:
            if column in dataframe.columns:
                dataframe = dataframe[dataframe[column] <= curr_date.replace("-", "")]
                break
    return cast(tuple[pd.DataFrame, str, str], (dataframe, market, symbol))


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str | None = None):
    try:
        dataframe, market, symbol = _fetch_statement(ticker, "balancesheet", curr_date)
        return format_dataframe_report(
            f"Tushare balance sheet for {ticker}",
            dataframe,
            {"Vendor": "tushare", "Market": market, "Vendor symbol": symbol, "Frequency": freq},
        )
    except Exception as exc:
        return f"Error retrieving balance sheet for {ticker} via tushare: {exc}"


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str | None = None):
    try:
        dataframe, market, symbol = _fetch_statement(ticker, "cashflow", curr_date)
        return format_dataframe_report(
            f"Tushare cash flow for {ticker}",
            dataframe,
            {"Vendor": "tushare", "Market": market, "Vendor symbol": symbol, "Frequency": freq},
        )
    except Exception as exc:
        return f"Error retrieving cash flow for {ticker} via tushare: {exc}"


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str | None = None):
    try:
        dataframe, market, symbol = _fetch_statement(ticker, "income", curr_date)
        return format_dataframe_report(
            f"Tushare income statement for {ticker}",
            dataframe,
            {"Vendor": "tushare", "Market": market, "Vendor symbol": symbol, "Frequency": freq},
        )
    except Exception as exc:
        return f"Error retrieving income statement for {ticker} via tushare: {exc}"
