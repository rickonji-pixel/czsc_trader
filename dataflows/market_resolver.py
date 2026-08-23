from __future__ import annotations

import re

MARKET_A_SHARE = "a_share"
MARKET_HK = "hk"
MARKET_US = "us"


def normalize_ticker(ticker: str) -> str:
    if not ticker or not isinstance(ticker, str):
        raise ValueError("Ticker must be a non-empty string.")
    return ticker.strip().upper()


def infer_a_share_exchange(code: str) -> str:
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"Invalid A-share code: {code}")
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("0", "1", "2", "3", "4", "8")):
        return "SZ"
    raise ValueError(f"Unable to infer A-share exchange for code: {code}")


def extract_a_share_code(ticker: str) -> str:
    value = normalize_ticker(ticker)
    if re.fullmatch(r"\d{6}", value):
        return value
    match = re.fullmatch(r"(?:SH|SZ|BJ)\.?(\d{6})", value)
    if match:
        return match.group(1)
    match = re.fullmatch(r"(\d{6})\.(?:SH|SZ|BJ)", value)
    if match:
        return match.group(1)
    raise ValueError(f"Unable to extract A-share code from ticker: {ticker}")


def extract_hk_code(ticker: str) -> str:
    value = normalize_ticker(ticker)
    if re.fullmatch(r"\d{4,5}", value):
        return value.zfill(5)
    match = re.fullmatch(r"HK\.(\d{4,5})", value)
    if match:
        return match.group(1).zfill(5)
    match = re.fullmatch(r"(\d{4,5})\.HK", value)
    if match:
        return match.group(1).zfill(5)
    raise ValueError(f"Unable to extract HK code from ticker: {ticker}")


def extract_us_code(ticker: str) -> str:
    value = normalize_ticker(ticker)
    if value.startswith("US."):
        return value.split(".", 1)[1]
    if re.fullmatch(r"\d+\.[A-Z][A-Z0-9.\-]*", value):
        return value
    if value.endswith(".US"):
        return value[:-3]
    return value


def detect_market(ticker: str) -> str:
    value = normalize_ticker(ticker)
    if re.fullmatch(r"\d{6}", value):
        return MARKET_A_SHARE
    if re.fullmatch(r"(?:SH|SZ|BJ)\.?\d{6}", value) or re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", value):
        return MARKET_A_SHARE
    if (
        re.fullmatch(r"\d{4,5}", value)
        or re.fullmatch(r"HK\.\d{4,5}", value)
        or re.fullmatch(r"\d{4,5}\.HK", value)
    ):
        return MARKET_HK
    return MARKET_US


def normalize_symbol_for_vendor(ticker: str, vendor: str, market: str | None = None) -> str:
    market = market or detect_market(ticker)
    vendor = vendor.lower()

    if market == MARKET_A_SHARE:
        code = extract_a_share_code(ticker)
        normalized_value = normalize_ticker(ticker)
        if normalized_value.endswith(".BJ") or normalized_value.startswith("BJ"):
            exchange = "BJ"
        else:
            exchange = infer_a_share_exchange(code)
        if vendor == "futu":
            return f"{exchange}.{code}"
        if vendor == "tushare":
            return f"{code}.{exchange}"
        if vendor == "akshare":
            return code
        if vendor == "finnhub":
            return code
        return code

    if market == MARKET_HK:
        code = extract_hk_code(ticker)
        if vendor == "futu":
            return f"HK.{code}"
        if vendor == "tushare":
            return f"{code}.HK"
        if vendor == "akshare":
            return code
        if vendor == "finnhub":
            return code
        return code

    code = extract_us_code(ticker)
    if vendor == "futu":
        return code if code.startswith("US.") else f"US.{code}"
    if vendor in {"tushare", "finnhub", "akshare"}:
        return code
    return code
