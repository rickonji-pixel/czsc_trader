"""Causal, auditable daily OHLCV snapshots for cross-asset research."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


CANONICAL_COLUMNS = ("dt", "symbol", "open", "high", "low", "close", "vol", "amount")
PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class DailySnapshot:
    symbol: str
    adjusted: pd.DataFrame
    execution: pd.DataFrame
    metadata: dict[str, object]


def canonicalize_daily(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalize supported research/vendor daily schemas without changing values."""

    aliases = {
        "Date": "dt",
        "date": "dt",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "vol",
        "volume": "vol",
        "Amount": "amount",
    }
    normalized_symbol = str(symbol).strip().upper()
    result = frame.rename(columns=aliases).copy()
    if "symbol" not in result:
        result["symbol"] = normalized_symbol
    missing = sorted(set(CANONICAL_COLUMNS).difference(result.columns))
    if missing:
        raise ValueError(f"{normalized_symbol}: daily frame missing columns {missing}")
    result = result.loc[:, CANONICAL_COLUMNS].copy()
    result["dt"] = pd.to_datetime(result["dt"], errors="coerce").dt.normalize()
    result["symbol"] = result["symbol"].astype(str).str.upper()
    for column in (*PRICE_COLUMNS, "vol", "amount"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if result.isna().any().any():
        raise ValueError(f"{normalized_symbol}: daily frame contains null values")
    if not result["symbol"].eq(normalized_symbol).all():
        raise ValueError(f"{normalized_symbol}: daily frame contains another symbol")
    if result["dt"].duplicated().any():
        raise ValueError(f"{normalized_symbol}: daily frame contains duplicate sessions")
    result = result.sort_values("dt").reset_index(drop=True)
    if not result["dt"].is_monotonic_increasing:
        raise ValueError(f"{normalized_symbol}: daily sessions are not increasing")
    return result


def validate_daily_pair(
    adjusted: pd.DataFrame,
    execution: pd.DataFrame,
    symbol: str,
) -> dict[str, object]:
    """Validate an adjusted/execution pair and return compact quality evidence."""

    adjusted = canonicalize_daily(adjusted, symbol)
    execution = canonicalize_daily(execution, symbol)
    if not adjusted["dt"].equals(execution["dt"]):
        raise ValueError(f"{symbol}: adjusted and execution sessions differ")
    for label, frame in (("adjusted", adjusted), ("execution", execution)):
        prices = frame.loc[:, PRICE_COLUMNS].to_numpy(dtype=float)
        if not np.isfinite(prices).all() or (prices <= 0).any():
            raise ValueError(f"{symbol}: {label} prices must be positive and finite")
        if (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any():
            raise ValueError(f"{symbol}: {label} high violates OHLC ordering")
        if (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any():
            raise ValueError(f"{symbol}: {label} low violates OHLC ordering")
        if (frame[["vol", "amount"]] < 0).any().any():
            raise ValueError(f"{symbol}: {label} volume or amount is negative")

    ratios = adjusted.loc[:, PRICE_COLUMNS].to_numpy(dtype=float) / execution.loc[
        :, PRICE_COLUMNS
    ].to_numpy(dtype=float)
    if not np.allclose(ratios, ratios[:, [0]], rtol=1e-8, atol=1e-10):
        raise ValueError(f"{symbol}: adjusted OHLC do not share one daily factor")
    if not np.allclose(
        adjusted["amount"].to_numpy(dtype=float),
        execution["amount"].to_numpy(dtype=float),
        rtol=1e-10,
        atol=1e-6,
    ):
        raise ValueError(f"{symbol}: adjusted and execution turnover amounts differ")
    return {
        "symbol": str(symbol).upper(),
        "sessions": int(len(adjusted)),
        "first_session": adjusted["dt"].min().date().isoformat(),
        "last_session": adjusted["dt"].max().date().isoformat(),
        "zero_volume_sessions": int(adjusted["vol"].eq(0).sum()),
        "adjustment_factor_min": float(ratios[:, 0].min()),
        "adjustment_factor_max": float(ratios[:, 0].max()),
    }


def synchronize_snapshots(
    snapshots: dict[str, DailySnapshot],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    require_exact_calendar: bool = True,
) -> tuple[dict[str, DailySnapshot], pd.DataFrame, dict[str, object]]:
    """Clip snapshots and enforce a common causal research calendar."""

    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    if start_ts > end_ts:
        raise ValueError("start must not be after end")
    clipped: dict[str, DailySnapshot] = {}
    session_sets: dict[str, set[pd.Timestamp]] = {}
    quality: dict[str, object] = {}
    for symbol, snapshot in sorted(snapshots.items()):
        adjusted = canonicalize_daily(snapshot.adjusted, symbol)
        execution = canonicalize_daily(snapshot.execution, symbol)
        adjusted = adjusted.loc[adjusted["dt"].between(start_ts, end_ts)].reset_index(drop=True)
        execution = execution.loc[execution["dt"].between(start_ts, end_ts)].reset_index(drop=True)
        if adjusted.empty:
            raise ValueError(f"{symbol}: no sessions in requested range")
        item_quality = validate_daily_pair(adjusted, execution, symbol)
        quality[symbol] = item_quality
        session_sets[symbol] = set(adjusted["dt"])
        clipped[symbol] = DailySnapshot(
            symbol=symbol,
            adjusted=adjusted,
            execution=execution,
            metadata=dict(snapshot.metadata),
        )

    common = set.intersection(*session_sets.values())
    union = set.union(*session_sets.values())
    missing = {
        symbol: sorted(ts.date().isoformat() for ts in union - sessions)
        for symbol, sessions in session_sets.items()
    }
    if require_exact_calendar and any(missing.values()):
        summary = {symbol: dates[:10] for symbol, dates in missing.items() if dates}
        raise ValueError(f"cross-asset trading calendars differ: {summary}")
    calendar = pd.DataFrame({"dt": sorted(common)})
    for symbol in sorted(clipped):
        calendar[symbol] = True
    report = {
        "requested_start": start_ts.date().isoformat(),
        "requested_end": end_ts.date().isoformat(),
        "common_sessions": int(len(common)),
        "common_first_session": min(common).date().isoformat(),
        "common_last_session": max(common).date().isoformat(),
        "exact_calendar_match": not any(missing.values()),
        "missing_sessions": missing,
        "symbols": quality,
    }
    return clipped, calendar, report


def align_adjusted_to_target_calendar(
    snapshots: dict[str, DailySnapshot],
    target_symbol: str,
    allowed_absences: dict[str, set[str]],
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Build a target-calendar panel using only information available on each date."""

    target_symbol = str(target_symbol).upper()
    if target_symbol not in snapshots:
        raise ValueError("target symbol is absent from snapshots")
    target = canonicalize_daily(snapshots[target_symbol].adjusted, target_symbol)
    target_calendar = pd.DatetimeIndex(target["dt"], name="dt")
    pieces: list[pd.DataFrame] = []
    audit: dict[str, object] = {}
    for symbol, snapshot in sorted(snapshots.items()):
        source = canonicalize_daily(snapshot.adjusted, symbol).set_index("dt")
        source_dates = set(source.index)
        missing_dates = set(target_calendar) - source_dates
        allowed = {pd.Timestamp(value).normalize() for value in allowed_absences.get(symbol, set())}
        unexpected = sorted(ts.date().isoformat() for ts in missing_dates - allowed)
        unused = sorted(ts.date().isoformat() for ts in allowed - missing_dates)
        if unexpected:
            raise ValueError(f"{symbol}: unapproved target-calendar absences {unexpected[:10]}")
        if unused:
            raise ValueError(f"{symbol}: absence allowlist does not match source {unused[:10]}")

        aligned = source.reindex(target_calendar).copy()
        observed = aligned["close"].notna()
        if not observed.iloc[0]:
            raise ValueError(f"{symbol}: first target session has no observation")
        source_dates_series = pd.Series(
            pd.to_datetime(aligned.index.where(observed)), index=aligned.index
        ).ffill()
        prior_close = aligned["close"].ffill()
        for column in PRICE_COLUMNS:
            aligned[column] = aligned[column].where(observed, prior_close)
        aligned["vol"] = aligned["vol"].where(observed, 0.0)
        aligned["amount"] = aligned["amount"].where(observed, 0.0)
        aligned["symbol"] = symbol
        aligned["observed"] = observed.to_numpy(dtype=bool)
        aligned["source_dt"] = source_dates_series.to_numpy()
        staleness: list[int] = []
        age = 0
        for is_observed in observed:
            age = 0 if is_observed else age + 1
            staleness.append(age)
        aligned["staleness_sessions"] = staleness
        aligned = aligned.reset_index().loc[
            :,
            (*CANONICAL_COLUMNS, "observed", "source_dt", "staleness_sessions"),
        ]
        pieces.append(aligned)
        audit[symbol] = {
            "target_sessions": int(len(aligned)),
            "observed_sessions": int(observed.sum()),
            "carried_sessions": int((~observed).sum()),
            "carried_dates": sorted(ts.date().isoformat() for ts in missing_dates),
            "maximum_staleness_sessions": int(max(staleness)),
        }

    panel = pd.concat(pieces, ignore_index=True).sort_values(["dt", "symbol"])
    return panel.reset_index(drop=True), {
        "target_symbol": target_symbol,
        "target_sessions": int(len(target_calendar)),
        "first_session": target_calendar.min().date().isoformat(),
        "last_session": target_calendar.max().date().isoformat(),
        "symbols": audit,
    }
