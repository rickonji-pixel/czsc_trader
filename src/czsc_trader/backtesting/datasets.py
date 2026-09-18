from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from pathlib import Path
from typing import Literal

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.data import (
    MarketData,
    load_execution_manifest,
    load_execution_prices,
    load_market_data,
)
from czsc_trader.intraday_data import load_intraday_research_data


DatasetName = Literal["research", "backtest"]


class ReplayDataNotReadyError(ValueError):
    """Requested replay window includes a trading session not yet published."""

    def __init__(
        self,
        *,
        requested_cutoff: date,
        published_cutoff: date,
        first_unpublished_session: date,
    ) -> None:
        self.requested_cutoff = requested_cutoff
        self.published_cutoff = published_cutoff
        self.first_unpublished_session = first_unpublished_session
        super().__init__(
            "backtest data is not ready: "
            f"requested cutoff {requested_cutoff.isoformat()} includes unpublished "
            f"trading session {first_unpublished_session.isoformat()}; "
            f"published cutoff is {published_cutoff.isoformat()}"
        )


@dataclass(frozen=True)
class ReplayData:
    dataset: DatasetName
    root: Path
    adjusted: MarketData
    execution_daily: pd.DataFrame
    execution_intraday: pd.DataFrame
    fingerprint: str
    cutoff: date
    execution_five_minute: pd.DataFrame | None = None
    signal_one_minute: pd.DataFrame | None = None


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
    execution_five_minute: pd.DataFrame | None = None,
    signal_one_minute: pd.DataFrame | None = None,
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
    if execution_five_minute is not None:
        digest.update(_frame_bytes(execution_five_minute))
        digest.update(b"\0")
    if signal_one_minute is not None:
        digest.update(_frame_bytes(signal_one_minute))
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


def _published_execution_boundary(
    root: Path,
    symbol: str,
    asset_type: str,
) -> tuple[date, date]:
    manifest = load_execution_manifest(root, symbol, asset_type)
    records = manifest.get("files")
    if not isinstance(records, dict) or not records:
        raise ValueError("execution-price manifest files must be a non-empty object")
    try:
        published_cutoff = max(
            pd.Timestamp(str(record["last"])).normalize()
            for record in records.values()
            if isinstance(record, dict)
        ).date()
        declared_cutoff = pd.Timestamp(str(manifest["requested_end"])).normalize().date()
        next_session = pd.Timestamp(
            str(manifest["next_trading_session"])
        ).normalize().date()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("execution-price manifest has an invalid publication boundary") from exc
    if declared_cutoff != published_cutoff:
        raise ValueError(
            "execution-price manifest requested_end differs from its latest published session"
        )
    if next_session <= published_cutoff:
        raise ValueError(
            "execution-price manifest next trading session must follow its published cutoff"
        )
    return published_cutoff, next_session


def _execution_five_minute(
    root: Path,
    adjusted: MarketData,
    execution_daily: pd.DataFrame,
    cutoff: date,
) -> pd.DataFrame:
    source = load_intraday_research_data(root, adjusted.symbol).frames["5m"].copy()
    source = source.loc[source["Date"].dt.normalize() <= pd.Timestamp(cutoff)].copy()
    adjusted_daily = adjusted.daily.set_index(pd.to_datetime(adjusted.daily["dt"]).dt.normalize())
    raw_daily = execution_daily.set_index(pd.to_datetime(execution_daily["dt"]).dt.normalize())
    factors = adjusted_daily["close"].astype(float).div(raw_daily["close"].astype(float))
    sessions = source["Date"].dt.normalize()
    row_factors = sessions.map(factors)
    if row_factors.isna().any() or row_factors.le(0).any():
        raise ValueError("5m execution prices have no positive daily adjustment factor")
    result = source.rename(
        columns={
            "Date": "dt",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "vol",
            "Amount": "amount",
        }
    )
    for column in ("open", "high", "low", "close"):
        result[column] = result[column].astype(float).div(row_factors.to_numpy(dtype=float))
    return result[["dt", "open", "high", "low", "close", "vol", "amount"]].reset_index(drop=True)


def load_replay_data(
    context: RepositoryContext,
    dataset: DatasetName,
    symbol: str,
    asset_type: str,
    cutoff: date,
    *,
    include_five_minute: bool = False,
    include_one_minute: bool = False,
) -> ReplayData:
    if dataset not in {"research", "backtest"}:
        raise ValueError(f"unknown replay dataset: {dataset}")
    root = context.research_data_root if dataset == "research" else context.backtest_data_root
    published_cutoff, next_session = _published_execution_boundary(
        root, symbol, asset_type
    )
    if cutoff >= next_session:
        raise ReplayDataNotReadyError(
            requested_cutoff=cutoff,
            published_cutoff=published_cutoff,
            first_unpublished_session=next_session,
        )
    adjusted = load_market_data(root, symbol, asset_type, cutoff=pd.Timestamp(cutoff))
    execution_daily = load_execution_prices(
        root,
        symbol,
        asset_type,
        cutoff=pd.Timestamp(cutoff),
    )
    execution_intraday = _execution_intraday(adjusted, execution_daily)
    effective_cutoff = pd.Timestamp(execution_daily["dt"].max()).date()
    execution_five_minute = (
        _execution_five_minute(root, adjusted, execution_daily, cutoff)
        if include_five_minute
        else None
    )
    signal_one_minute = None
    if include_one_minute:
        source = load_intraday_research_data(root, adjusted.symbol).frames["1m"].copy()
        source = source.loc[source["Date"].dt.normalize() <= pd.Timestamp(cutoff)].copy()
        signal_one_minute = source.rename(
            columns={
                "Date": "dt",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "vol",
                "Amount": "amount",
            }
        )[["dt", "open", "high", "low", "close", "vol", "amount"]].reset_index(drop=True)
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
            execution_five_minute,
            signal_one_minute,
        ),
        cutoff=effective_cutoff,
        execution_five_minute=execution_five_minute,
        signal_one_minute=signal_one_minute,
    )
