"""Tushare repair patch for 518800.SH."""

from __future__ import annotations

from typing import Mapping, Sequence

import pandas as pd

from ..errors import DataRepairError
from ..history_validation import ValidationFinding
from .common import configured_repair_dates, replace_intraday_from_1m
from .model import RepairPatch, SeriesKey


_30M_SERIES = SeriesKey(
    "tushare", "etf_mins", "518800.SH", "etf.ohlcv", "30m", "none"
)
_REBUILD_FINDINGS = {
    "INVALID_OHLCV",
    "INCOMPLETE_TRADING_SESSION",
    "TRADING_DATE_MISMATCH",
    "CROSS_FREQUENCY_MISMATCH",
}

_30M_REBUILD_DATES = (
    "2013-07-29",
    "2013-08-19",
    "2013-08-21",
    "2013-10-22",
    "2013-11-07",
    "2014-01-03",
    "2014-01-08",
    "2014-01-10",
    "2014-01-14",
    "2014-01-15",
    "2014-01-20",
    "2014-01-22",
    "2014-01-24",
    "2014-01-27",
    "2014-02-07",
    "2014-02-18",
    "2014-04-03",
    "2014-04-18",
    "2014-04-22",
    "2014-04-28",
    "2014-04-29",
    "2014-05-05",
    "2014-05-09",
    "2014-05-12",
    "2014-05-13",
    "2014-05-14",
    "2014-05-16",
    "2014-05-19",
    "2014-05-22",
    "2014-05-23",
    "2014-05-26",
    "2014-05-27",
    "2014-05-28",
    "2014-05-29",
    "2014-05-30",
    "2014-06-03",
    "2014-06-05",
    "2014-06-06",
    "2014-06-09",
    "2014-06-10",
    "2014-06-13",
    "2014-06-17",
    "2014-06-19",
    "2014-06-23",
    "2014-06-25",
    "2014-06-26",
    "2014-06-30",
    "2014-07-01",
    "2014-07-02",
    "2014-07-03",
    "2014-07-04",
    "2014-07-09",
    "2014-07-15",
    "2014-07-17",
    "2014-07-22",
    "2014-07-23",
    "2014-07-24",
    "2014-07-28",
    "2014-07-29",
    "2014-07-30",
    "2014-07-31",
    "2014-08-01",
    "2014-08-04",
    "2014-08-05",
    "2014-08-06",
    "2014-08-08",
    "2014-08-11",
    "2014-08-15",
    "2014-08-18",
    "2014-08-19",
    "2014-08-20",
    "2014-08-21",
    "2014-08-22",
    "2014-08-26",
    "2014-08-27",
    "2014-08-28",
    "2014-09-02",
    "2014-09-11",
    "2014-09-15",
    "2014-09-17",
    "2014-09-19",
    "2014-09-22",
    "2014-09-24",
    "2014-09-30",
    "2014-10-09",
    "2014-10-14",
    "2014-10-29",
    "2014-10-30",
    "2014-11-06",
    "2014-11-19",
    "2014-11-20",
    "2014-12-05",
    "2014-12-11",
    "2014-12-15",
    "2014-12-16",
    "2014-12-19",
    "2014-12-24",
    "2014-12-30",
    "2015-01-06",
    "2015-01-07",
    "2015-01-09",
    "2015-01-13",
    "2015-01-14",
    "2015-01-19",
    "2015-02-02",
    "2015-02-03",
    "2015-02-04",
    "2015-02-05",
    "2015-02-11",
    "2015-02-12",
    "2015-02-13",
    "2015-02-16",
    "2015-02-17",
    "2015-02-25",
    "2015-02-26",
    "2015-03-03",
    "2015-03-05",
    "2015-03-06",
    "2015-03-10",
    "2015-04-01",
    "2015-04-17",
    "2015-04-20",
    "2015-05-06",
    "2015-06-17",
    "2015-06-29",
    "2015-07-01",
    "2015-07-02",
    "2015-07-07",
    "2015-07-15",
    "2015-07-16",
    "2015-07-20",
    "2015-07-22",
    "2015-07-28",
    "2015-07-29",
    "2015-07-30",
    "2015-08-03",
    "2015-08-04",
    "2015-08-07",
    "2015-08-10",
    "2015-08-11",
    "2015-08-26",
    "2015-08-28",
    "2015-08-31",
    "2015-09-01",
    "2015-09-08",
    "2015-09-14",
    "2015-09-15",
    "2015-09-16",
    "2015-09-18",
    "2015-09-22",
    "2015-09-29",
    "2015-09-30",
    "2015-10-09",
    "2015-10-12",
    "2015-10-13",
    "2015-10-19",
    "2015-10-20",
    "2015-10-22",
    "2015-10-27",
    "2015-10-28",
    "2015-10-29",
    "2015-11-03",
    "2015-11-04",
    "2015-11-05",
    "2015-11-06",
    "2015-11-12",
    "2015-11-13",
    "2015-11-19",
    "2015-11-20",
    "2015-11-23",
    "2015-11-24",
    "2015-12-01",
    "2015-12-08",
    "2015-12-11",
    "2015-12-14",
    "2015-12-17",
    "2015-12-22",
    "2015-12-25",
    "2016-01-06",
    "2016-01-13",
    "2016-01-26",
    "2016-01-29",
    "2016-02-05",
    "2016-03-25",
    "2016-04-08",
    "2016-04-26",
    "2016-06-23",
    "2016-06-27",
    "2016-07-01",
    "2016-07-06",
    "2016-08-03",
    "2016-08-09",
    "2016-08-11",
    "2016-11-28",
    "2016-12-05",
    "2017-02-03",
    "2017-09-07",
    "2017-12-28",
    "2018-05-16",
    "2018-12-18",
    "2019-01-04",
    "2019-05-14",
    "2019-07-01",
    "2019-08-14",
    "2019-08-19",
    "2019-09-16",
    "2019-12-17",
    "2020-01-08",
    "2020-01-09",
    "2020-02-27",
    "2020-12-17",
    "2021-01-11",
    "2021-01-14",
    "2022-10-20",
    "2023-05-17",
    "2023-11-20",
    "2023-11-22",
    "2024-03-25",
    "2024-04-03",
    "2024-04-19",
    "2024-04-26",
    "2024-04-30",
    "2024-05-24",
    "2024-05-31",
    "2024-06-14",
)

# After rebuilding from the independent 1m source, these exact daily totals or
# extrema remain absent from the minute history. Align only the registered
# fields to the independently fetched daily reference.
_DAILY_ALIGNMENT_FIELDS = {
    "2013-11-07": ("High",),
    "2014-02-18": ("Volume",),
    "2014-06-06": ("Low",),
    "2014-06-30": ("High",),
    "2014-07-24": ("High",),
    "2014-07-28": ("Low",),
    "2015-02-05": ("Low", "Amount"),
    "2019-01-04": ("Low",),
    "2019-05-14": ("Low",),
    "2019-07-01": ("High",),
    "2019-08-14": ("Low",),
    "2019-08-19": ("Low",),
    "2019-09-16": ("High",),
    "2020-01-08": ("High",),
    "2020-01-09": ("Low",),
    "2020-02-27": ("Low",),
    "2021-01-11": ("Low",),
    "2021-01-14": ("Low",),
}


def _align_activity(day: pd.DataFrame, column: str, target: float) -> None:
    current = float(day[column].sum())
    if current <= 0:
        day.loc[day.index[-1], column] = target
        return
    day.loc[:, column] = day[column] * (target / current)
    residual = target - float(day[column].sum())
    day.loc[day.index[-1], column] += residual


def _align_registered_daily_fields(
    dataframe: pd.DataFrame,
    daily: pd.DataFrame,
) -> pd.DataFrame:
    frame = dataframe.copy()
    frame_dates = pd.to_datetime(frame["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily_frame = daily.copy()
    daily_frame["_date"] = pd.to_datetime(
        daily_frame["Date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    daily_index = daily_frame.set_index("_date")
    for trade_date, fields in _DAILY_ALIGNMENT_FIELDS.items():
        indices = frame.index[frame_dates == trade_date]
        if indices.empty:
            continue
        if trade_date not in daily_index.index:
            raise DataRepairError(f"{trade_date}: daily repair reference is missing")
        reference = daily_index.loc[trade_date]
        day = frame.loc[indices].copy()
        for field in fields:
            target = float(reference[field])
            if field == "High":
                frame.loc[indices, ["Open", "High", "Low", "Close"]] = frame.loc[
                    indices, ["Open", "High", "Low", "Close"]
                ].clip(upper=target)
                target_index = frame.loc[indices, "High"].idxmax()
                frame.at[target_index, "High"] = target
            elif field == "Low":
                frame.loc[indices, ["Open", "High", "Low", "Close"]] = frame.loc[
                    indices, ["Open", "High", "Low", "Close"]
                ].clip(lower=target)
                target_index = frame.loc[indices, "Low"].idxmin()
                frame.at[target_index, "Low"] = target
            elif field in {"Volume", "Amount"}:
                _align_activity(day, field, target)
                frame.loc[indices, field] = day[field]
            else:
                raise DataRepairError(f"unsupported daily alignment field: {field}")
    return frame


def _reference_dates(
    daily: pd.DataFrame,
    patch: RepairPatch,
    series: SeriesKey,
    findings: Sequence[ValidationFinding],
) -> tuple[str, ...]:
    del patch
    if series != _30M_SERIES or not {
        item.code for item in findings
    }.intersection(_REBUILD_FINDINGS):
        return ()
    return configured_repair_dates(daily, _30M_REBUILD_DATES)


def _execute(
    dataframe: pd.DataFrame,
    patch: RepairPatch,
    series: SeriesKey,
    findings: Sequence[ValidationFinding],
    references: Mapping[str, pd.DataFrame],
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    if series != _30M_SERIES or not {
        item.code for item in findings
    }.intersection(_REBUILD_FINDINGS):
        return dataframe.copy(), ()
    repair_dates = configured_repair_dates(
        references.get("daily"), _30M_REBUILD_DATES
    )
    repaired, affected = replace_intraday_from_1m(
        dataframe, patch, series, references, repair_dates
    )
    return _align_registered_daily_fields(repaired, references["daily"]), affected


PATCH = RepairPatch(
    "TUSHARE_518800_V1",
    1,
    "tushare",
    "518800.SH",
    _execute,
    reference_dates=_reference_dates,
)
