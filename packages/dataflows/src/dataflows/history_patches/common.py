"""Mechanics shared by isolated vendor-symbol repair patches."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping, Sequence

import pandas as pd

from ..bar_utils import INTRADAY_PERIOD_MINUTES, a_share_intraday_close_times
from ..errors import DataRepairError
from .model import RepairPatch, SeriesKey


def frame_content_sha256(dataframe: pd.DataFrame) -> str:
    digest = sha256()
    schema = [(str(column), str(dtype)) for column, dtype in dataframe.dtypes.items()]
    digest.update(repr(schema).encode("utf-8"))
    digest.update(pd.util.hash_pandas_object(dataframe, index=True).values.tobytes())
    return digest.hexdigest()


def configured_repair_dates(
    dataframe: pd.DataFrame | None,
    configured_dates: Sequence[str],
) -> tuple[str, ...]:
    if dataframe is None:
        return ()
    observed = pd.to_datetime(dataframe["Date"], errors="coerce").dt.strftime(
        "%Y-%m-%d"
    )
    return tuple(
        sorted(observed.loc[observed.isin(configured_dates)].dropna().unique().tolist())
    )


def rebuild_intraday_from_1m(
    one_minute: pd.DataFrame,
    daily: pd.DataFrame,
    frequency: str,
    *,
    dates: Sequence[str],
) -> pd.DataFrame:
    if frequency not in INTRADAY_PERIOD_MINUTES or frequency == "1m":
        raise DataRepairError(f"cannot rebuild unsupported frequency {frequency}")
    minutes = INTRADAY_PERIOD_MINUTES[frequency]
    if 120 % minutes:
        raise DataRepairError(f"{frequency}: session length is not divisible by period")
    minute = one_minute.copy()
    minute["Date"] = pd.to_datetime(minute["Date"], errors="coerce")
    daily_frame = daily.copy()
    daily_frame["Date"] = pd.to_datetime(
        daily_frame["Date"], errors="coerce"
    ).dt.normalize()
    if minute["Date"].isna().any() or daily_frame["Date"].isna().any():
        raise DataRepairError("repair references contain invalid timestamps")
    if daily_frame["Date"].duplicated().any():
        raise DataRepairError("daily repair reference contains duplicate dates")
    daily_indexed = daily_frame.set_index("Date")
    expected_times = set(a_share_intraday_close_times("1m"))
    requested = {pd.Timestamp(item).normalize() for item in dates}
    pieces: list[pd.DataFrame] = []
    trade_dates = minute["Date"].dt.normalize()
    for trade_date in sorted(requested):
        if trade_date not in daily_indexed.index:
            raise DataRepairError(f"{trade_date.date()}: daily repair reference is missing")
        day = minute.loc[trade_dates == trade_date].sort_values("Date").reset_index(drop=True)
        clocks = day["Date"].dt.strftime("%H:%M:%S")
        regular = day.loc[clocks.isin(expected_times)].reset_index(drop=True)
        if len(regular) != 240 or set(
            regular["Date"].dt.strftime("%H:%M:%S")
        ) != expected_times:
            raise DataRepairError(
                f"{trade_date.date()}: expected 240 complete 1m repair bars, found {len(regular)}"
            )
        opening = day.loc[clocks.eq("09:30:00")]
        if len(opening) > 1:
            raise DataRepairError(f"{trade_date.date()}: duplicate 09:30 auction rows")
        active_opening = opening.loc[(opening["Volume"] > 0) | (opening["Amount"] > 0)]
        day_rows: list[dict[str, Any]] = []
        for session_number, session in enumerate((regular.iloc[:120], regular.iloc[120:])):
            group_ids = pd.Series(range(120), index=session.index) // minutes
            for group_number, bucket in session.groupby(group_ids, sort=True):
                active = bucket.loc[(bucket["Volume"] > 0) | (bucket["Amount"] > 0)]
                if session_number == 0 and group_number == 0 and not active_opening.empty:
                    active = pd.concat([active_opening, active], ignore_index=True)
                price_rows = active if not active.empty else bucket
                day_rows.append(
                    {
                        "Date": bucket.iloc[-1]["Date"],
                        "Open": price_rows.iloc[0]["Open"],
                        "High": price_rows["High"].max(),
                        "Low": price_rows["Low"].min(),
                        "Close": price_rows.iloc[-1]["Close"],
                        "Volume": bucket["Volume"].sum()
                        + (
                            active_opening["Volume"].sum()
                            if session_number == 0 and group_number == 0
                            else 0
                        ),
                        "Amount": bucket["Amount"].sum()
                        + (
                            active_opening["Amount"].sum()
                            if session_number == 0 and group_number == 0
                            else 0
                        ),
                    }
                )
        rebuilt = pd.DataFrame(day_rows)
        daily_row = daily_indexed.loc[trade_date]
        rebuilt.at[rebuilt.index[0], "Open"] = daily_row["Open"]
        rebuilt.at[rebuilt.index[-1], "Close"] = daily_row["Close"]
        rebuilt.at[rebuilt.index[0], "High"] = max(
            rebuilt.iloc[0]["High"], daily_row["Open"]
        )
        rebuilt.at[rebuilt.index[0], "Low"] = min(
            rebuilt.iloc[0]["Low"], daily_row["Open"]
        )
        rebuilt.at[rebuilt.index[-1], "High"] = max(
            rebuilt.iloc[-1]["High"], daily_row["Close"]
        )
        rebuilt.at[rebuilt.index[-1], "Low"] = min(
            rebuilt.iloc[-1]["Low"], daily_row["Close"]
        )
        pieces.append(rebuilt)
    if not pieces:
        return one_minute.iloc[0:0].copy()
    return pd.concat(pieces, ignore_index=True).sort_values("Date").reset_index(drop=True)


def replace_intraday_from_1m(
    dataframe: pd.DataFrame,
    patch: RepairPatch,
    series: SeriesKey,
    references: Mapping[str, pd.DataFrame],
    repair_dates: Sequence[str],
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    if "daily" not in references:
        raise DataRepairError(
            f"{patch.patch_id}: 1m and daily repair references are required"
        )
    if not repair_dates:
        return dataframe.copy(), ()
    if "1m" not in references:
        raise DataRepairError(
            f"{patch.patch_id}: 1m and daily repair references are required"
        )
    rebuilt = rebuild_intraday_from_1m(
        references["1m"], references["daily"], series.frequency, dates=repair_dates
    )
    current_dates = pd.to_datetime(dataframe["Date"], errors="coerce").dt.strftime(
        "%Y-%m-%d"
    )
    repaired = pd.concat(
        [dataframe.loc[~current_dates.isin(repair_dates)], rebuilt],
        ignore_index=True,
    )
    repaired["Date"] = pd.to_datetime(repaired["Date"], errors="raise")
    repaired = repaired.sort_values("Date").reset_index(drop=True)
    return repaired, tuple(repair_dates)
