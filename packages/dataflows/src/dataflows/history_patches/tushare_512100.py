"""Tushare repair patch for 512100.SH."""

from __future__ import annotations

from typing import Mapping, Sequence

import pandas as pd

from ..history_validation import ValidationFinding
from .model import RepairPatch, SeriesKey


_VOLUME_X100_DATES = (
    "2024-04-03",
    "2024-04-19",
    "2024-04-26",
    "2024-04-30",
    "2024-05-24",
    "2024-05-31",
    "2024-06-14",
)


def _scale_volume_x100(
    dataframe: pd.DataFrame,
    series: SeriesKey,
    findings: Sequence[ValidationFinding],
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    if not (
        series.endpoint == "etf_mins"
        and series.dataset == "etf.ohlcv"
        and series.adjustment == "none"
        and series.frequency in {"5m", "15m", "30m"}
    ):
        return dataframe.copy(), ()
    mismatched: set[str] = set()
    for finding in findings:
        if finding.code != "CROSS_FREQUENCY_MISMATCH":
            continue
        for trade_date, fields in finding.context.get("fields_by_date", {}).items():
            if fields == ["Volume"]:
                mismatched.add(str(trade_date))
    affected = sorted(set(_VOLUME_X100_DATES).intersection(mismatched))
    if not affected:
        return dataframe.copy(), ()
    frame = dataframe.copy()
    dates = pd.to_datetime(frame["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame.loc[dates.isin(affected), "Volume"] /= 100.0
    return frame, tuple(affected)


def _execute(
    dataframe: pd.DataFrame,
    patch: RepairPatch,
    series: SeriesKey,
    findings: Sequence[ValidationFinding],
    references: Mapping[str, pd.DataFrame],
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    del patch, references
    return _scale_volume_x100(dataframe, series, findings)


PATCH = RepairPatch(
    "TUSHARE_512100_V1",
    1,
    "tushare",
    "512100.SH",
    _execute,
)
