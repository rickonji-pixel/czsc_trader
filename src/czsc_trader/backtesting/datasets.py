from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from pathlib import Path
from typing import Literal

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.data import MarketData, load_execution_prices, load_market_data


DatasetName = Literal["research", "backtest"]


@dataclass(frozen=True)
class ReplayData:
    dataset: DatasetName
    root: Path
    adjusted: MarketData
    execution_daily: pd.DataFrame
    execution_intraday: pd.DataFrame
    fingerprint: str
    cutoff: date


def _frame_bytes(frame: pd.DataFrame) -> bytes:
    value = frame.copy()
    if "dt" in value:
        value["dt"] = pd.to_datetime(value["dt"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
    return value.to_csv(index=False, lineterminator="\n", float_format="%.12g").encode("utf-8")


def _fingerprint(
    dataset: DatasetName,
    adjusted: MarketData,
    execution_daily: pd.DataFrame,
    execution_intraday: pd.DataFrame,
) -> str:
    digest = hashlib.sha256()
    for value in (dataset, adjusted.symbol, adjusted.asset_type):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for frame in (
        adjusted.intraday,
        adjusted.daily,
        adjusted.weekly,
        execution_daily,
        execution_intraday,
    ):
        digest.update(_frame_bytes(frame))
        digest.update(b"\0")
    return digest.hexdigest()


def _execution_intraday(
    adjusted: MarketData,
    execution_daily: pd.DataFrame,
) -> pd.DataFrame:
    adjusted_daily = adjusted.daily.set_index(pd.to_datetime(adjusted.daily["dt"]).dt.normalize())
    raw_daily = execution_daily.set_index(pd.to_datetime(execution_daily["dt"]).dt.normalize())
    if not adjusted_daily.index.equals(raw_daily.index):
        raise ValueError("adjusted and execution daily sessions differ")
    factors = adjusted_daily["close"].astype(float).div(raw_daily["close"].astype(float))
    if factors.isna().any() or factors.le(0).any():
        raise ValueError("daily adjustment factors must be positive and complete")
    result = adjusted.intraday.copy()
    sessions = pd.to_datetime(result["dt"]).dt.normalize()
    row_factors = sessions.map(factors)
    if row_factors.isna().any():
        raise ValueError("intraday execution prices have no daily adjustment factor")
    for column in ("open", "high", "low", "close"):
        result[column] = result[column].astype(float).div(row_factors.to_numpy(dtype=float))
    return result


def load_replay_data(
    context: RepositoryContext,
    dataset: DatasetName,
    symbol: str,
    asset_type: str,
    cutoff: date,
) -> ReplayData:
    if dataset not in {"research", "backtest"}:
        raise ValueError(f"unknown replay dataset: {dataset}")
    root = context.research_data_root if dataset == "research" else context.backtest_data_root
    adjusted = load_market_data(root, symbol, asset_type, cutoff=pd.Timestamp(cutoff))
    execution_daily = load_execution_prices(
        root,
        symbol,
        asset_type,
        cutoff=pd.Timestamp(cutoff),
    )
    execution_intraday = _execution_intraday(adjusted, execution_daily)
    return ReplayData(
        dataset=dataset,
        root=root,
        adjusted=adjusted,
        execution_daily=execution_daily,
        execution_intraday=execution_intraday,
        fingerprint=_fingerprint(
            dataset,
            adjusted,
            execution_daily,
            execution_intraday,
        ),
        cutoff=cutoff,
    )
