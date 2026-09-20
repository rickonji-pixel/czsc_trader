"""Explicit, source-bound repairs for historical market-data findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import pandas as pd

from .bar_utils import INTRADAY_PERIOD_MINUTES, a_share_intraday_close_times
from .errors import DataRepairError
from .history_validation import ValidationFinding


@dataclass(frozen=True, slots=True)
class SeriesKey:
    vendor: str
    endpoint: str
    symbol: str
    dataset: str
    frequency: str
    adjustment: str


@dataclass(frozen=True, slots=True)
class RepairBinding:
    """Static authorization to route exact findings to one repair algorithm."""

    binding_id: str
    series: SeriesKey
    valid_from: str
    valid_through: str
    finding_codes: tuple[str, ...]
    algorithm: str
    algorithm_version: int
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """Immutable evidence produced by one actual repair-algorithm execution."""

    binding_id: str
    algorithm: str
    algorithm_version: int
    findings_before: tuple[str, ...]
    affected_dates: tuple[str, ...]
    raw_content_sha256: str
    repaired_content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "binding_id": self.binding_id,
            "algorithm": self.algorithm,
            "algorithm_version": self.algorithm_version,
            "findings_before": list(self.findings_before),
            "affected_date_count": len(self.affected_dates),
            "affected_date_first": self.affected_dates[0],
            "affected_date_last": self.affected_dates[-1],
            "raw_content_sha256": self.raw_content_sha256,
            "repaired_content_sha256": self.repaired_content_sha256,
        }
        if len(self.affected_dates) <= 50:
            payload["affected_dates"] = list(self.affected_dates)
        return payload


def frame_content_sha256(dataframe: pd.DataFrame) -> str:
    """Hash normalized content, schema, column order and index deterministically."""

    digest = sha256()
    schema = [(str(column), str(dtype)) for column, dtype in dataframe.dtypes.items()]
    digest.update(repr(schema).encode("utf-8"))
    digest.update(pd.util.hash_pandas_object(dataframe, index=True).values.tobytes())
    return digest.hexdigest()


_DAILY_518880_CORRECTIONS = {
    "2013-11-07": {
        "Volume": (2269500.0, 2410300.0),
        "Amount": (5865485.0, 6230016.0),
    },
    "2016-03-01": {"Low": (2.611, 2.620)},
    "2019-09-10": {"Low": (3.371, 3.378)},
    "2019-11-01": {"Low": (3.300, 3.379)},
    "2020-03-09": {"Low": (3.317, 3.548)},
    "2020-08-07": {"Low": (4.315, 4.339)},
    "2021-03-03": {"High": (3.572, 3.556)},
}

_VOLUME_X100_DATES = (
    "2024-04-03",
    "2024-04-19",
    "2024-04-26",
    "2024-04-30",
    "2024-05-24",
    "2024-05-31",
    "2024-06-14",
)

_REBUILD_518880_30M_DATES = (
    "2013-08-02",
    "2013-11-01",
    "2013-11-08",
    "2013-11-27",
    "2013-11-28",
    "2014-01-29",
    "2014-02-07",
    "2014-04-10",
    "2014-04-23",
    "2014-05-16",
    "2014-05-19",
    "2014-05-26",
    "2014-05-27",
    "2014-05-30",
    "2014-06-03",
    "2014-06-06",
    "2014-06-11",
    "2014-07-09",
    "2014-08-19",
    "2014-08-26",
    "2014-12-31",
    "2015-01-29",
    "2015-02-11",
    "2015-06-17",
    "2015-07-16",
    "2015-08-19",
    "2015-09-02",
    "2016-06-24",
    "2017-08-07",
    "2018-12-18",
    "2024-04-03",
    "2024-04-19",
    "2024-04-26",
    "2024-04-30",
    "2024-05-24",
    "2024-05-31",
    "2024-06-14",
)


def _volume_bindings() -> tuple[RepairBinding, ...]:
    return tuple(
        RepairBinding(
            binding_id=f"TUSHARE_{symbol.split('.', 1)[0]}_{frequency.upper()}_VOLUME_X100_V1",
            series=SeriesKey(
                "tushare", "etf_mins", symbol, "etf.ohlcv", frequency, "none"
            ),
            valid_from="2024-04-03",
            valid_through="2024-06-14",
            finding_codes=("CROSS_FREQUENCY_MISMATCH",),
            algorithm="SCALE_VOLUME_ON_DATES",
            algorithm_version=1,
            parameters={"dates": _VOLUME_X100_DATES, "divisor": 100.0},
        )
        for symbol in ("510500.SH", "512100.SH", "515050.SH", "588080.SH")
        for frequency in ("5m", "15m", "30m")
    )


REPAIR_BINDINGS: tuple[RepairBinding, ...] = (
    RepairBinding(
        binding_id="TUSHARE_518880_DAILY_FIELDS_V1",
        series=SeriesKey(
            "tushare", "fund_daily", "518880.SH", "etf.ohlcv", "daily", "none"
        ),
        valid_from="2013-11-07",
        valid_through="2021-03-03",
        finding_codes=("KNOWN_SOURCE_ANOMALY",),
        algorithm="REPLACE_EXACT_FIELDS",
        algorithm_version=1,
        parameters={"corrections": _DAILY_518880_CORRECTIONS},
    ),
    RepairBinding(
        binding_id="TUSHARE_518880_30M_HISTORY_V1",
        series=SeriesKey(
            "tushare", "etf_mins", "518880.SH", "etf.ohlcv", "30m", "none"
        ),
        valid_from="2013-07-29",
        valid_through="2026-09-18",
        finding_codes=(
            "INVALID_OHLCV",
            "INCOMPLETE_TRADING_SESSION",
            "TRADING_DATE_MISMATCH",
            "CROSS_FREQUENCY_MISMATCH",
        ),
        algorithm="REBUILD_INTRADAY_FROM_1M",
        algorithm_version=1,
        parameters={"dates": _REBUILD_518880_30M_DATES},
    ),
    RepairBinding(
        binding_id="TUSHARE_510500_15M_20241030_V1",
        series=SeriesKey(
            "tushare", "etf_mins", "510500.SH", "etf.ohlcv", "15m", "none"
        ),
        valid_from="2024-10-30",
        valid_through="2024-10-30",
        finding_codes=("TRADING_DATE_MISMATCH",),
        algorithm="REBUILD_INTRADAY_FROM_1M",
        algorithm_version=1,
        parameters={"dates": ("2024-10-30",)},
    ),
    RepairBinding(
        binding_id="TUSHARE_588080_15M_20241030_V1",
        series=SeriesKey(
            "tushare", "etf_mins", "588080.SH", "etf.ohlcv", "15m", "none"
        ),
        valid_from="2024-10-30",
        valid_through="2024-10-30",
        finding_codes=("TRADING_DATE_MISMATCH",),
        algorithm="REBUILD_INTRADAY_FROM_1M",
        algorithm_version=1,
        parameters={"dates": ("2024-10-30",)},
    ),
    *_volume_bindings(),
)


def validate_repair_registry(
    bindings: Sequence[RepairBinding] = REPAIR_BINDINGS,
) -> None:
    """Reject duplicate or ambiguous source-series repair bindings."""

    binding_ids = [binding.binding_id for binding in bindings]
    if len(binding_ids) != len(set(binding_ids)):
        raise DataRepairError("repair registry contains duplicate binding ids")
    for index, left in enumerate(bindings):
        for right in bindings[index + 1 :]:
            if left.series != right.series:
                continue
            overlaps = not (
                left.valid_through < right.valid_from
                or right.valid_through < left.valid_from
            )
            shared_findings = set(left.finding_codes).intersection(right.finding_codes)
            if overlaps and shared_findings:
                raise DataRepairError(
                    "repair registry contains ambiguous bindings",
                    left=left.binding_id,
                    right=right.binding_id,
                    finding_codes=sorted(shared_findings),
                )


validate_repair_registry()


def bindings_for(series: SeriesKey) -> tuple[RepairBinding, ...]:
    return tuple(binding for binding in REPAIR_BINDINGS if binding.series == series)


def inspect_registered_source_anomalies(
    dataframe: pd.DataFrame,
    series: SeriesKey,
) -> tuple[ValidationFinding, ...]:
    """Turn exact known bad source signatures into validation failures."""

    if dataframe.empty:
        return ()
    timestamps = pd.to_datetime(dataframe["Date"], errors="coerce")
    dates = timestamps.dt.strftime("%Y-%m-%d")
    findings: list[ValidationFinding] = []
    for binding in bindings_for(series):
        if binding.algorithm != "REPLACE_EXACT_FIELDS":
            continue
        corrections = binding.parameters.get("corrections", {})
        for trade_date, fields in corrections.items():
            indices = dataframe.index[dates == trade_date]
            if indices.empty:
                continue
            if len(indices) != 1:
                findings.append(
                    ValidationFinding(
                        "SOURCE_SIGNATURE_UNKNOWN",
                        f"{series.symbol} {trade_date}: expected one source row",
                        {"binding_id": binding.binding_id, "row_count": len(indices)},
                    )
                )
                continue
            index = indices[0]
            bad_fields: list[str] = []
            unknown_fields: list[str] = []
            for column, values in fields.items():
                expected_bad, accepted_good = values
                actual = float(dataframe.at[index, column])
                if abs(actual - float(expected_bad)) <= 1e-9:
                    bad_fields.append(column)
                elif abs(actual - float(accepted_good)) > 1e-9:
                    unknown_fields.append(column)
            if unknown_fields:
                findings.append(
                    ValidationFinding(
                        "SOURCE_SIGNATURE_UNKNOWN",
                        f"{series.symbol} {trade_date}: source signature changed",
                        {
                            "binding_id": binding.binding_id,
                            "date": trade_date,
                            "fields": unknown_fields,
                        },
                    )
                )
            elif bad_fields:
                findings.append(
                    ValidationFinding(
                        "KNOWN_SOURCE_ANOMALY",
                        f"{series.symbol} {trade_date}: known source anomaly",
                        {
                            "binding_id": binding.binding_id,
                            "date": trade_date,
                            "fields": bad_fields,
                        },
                    )
                )
    return tuple(findings)


def repair_dates_for(
    dataframe: pd.DataFrame, binding: RepairBinding
) -> tuple[str, ...]:
    """Return dates in the requested reference frame authorized by one binding."""

    timestamps = pd.to_datetime(dataframe["Date"], errors="coerce")
    observed = timestamps.dt.strftime("%Y-%m-%d")
    mask = observed.between(binding.valid_from, binding.valid_through)
    configured = binding.parameters.get("dates")
    if configured:
        mask &= observed.isin(configured)
    return tuple(sorted(observed.loc[mask].dropna().unique().tolist()))


def _replace_exact_fields(
    dataframe: pd.DataFrame,
    binding: RepairBinding,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    frame = dataframe.copy()
    dates = pd.to_datetime(frame["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    affected: list[str] = []
    for trade_date, fields in binding.parameters["corrections"].items():
        indices = frame.index[dates == trade_date]
        if indices.empty:
            continue
        index = indices[0]
        changed = False
        for column, (expected_bad, replacement) in fields.items():
            actual = float(frame.at[index, column])
            if abs(actual - float(expected_bad)) > 1e-9:
                continue
            frame.at[index, column] = replacement
            changed = True
        if changed:
            affected.append(trade_date)
    return frame, tuple(affected)


def _scale_volume_on_dates(
    dataframe: pd.DataFrame,
    binding: RepairBinding,
    findings: Sequence[ValidationFinding],
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    configured = set(binding.parameters["dates"])
    mismatched: set[str] = set()
    for finding in findings:
        if finding.code != "CROSS_FREQUENCY_MISMATCH":
            continue
        for trade_date, fields in finding.context.get("fields_by_date", {}).items():
            if fields == ["Volume"] or ("Volume" in fields and len(fields) == 1):
                mismatched.add(str(trade_date))
    affected = sorted(configured.intersection(mismatched))
    if not affected:
        return dataframe.copy(), ()
    frame = dataframe.copy()
    dates = pd.to_datetime(frame["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    mask = dates.isin(affected)
    frame.loc[mask, "Volume"] = frame.loc[mask, "Volume"] / float(
        binding.parameters["divisor"]
    )
    return frame, tuple(affected)


def rebuild_intraday_from_1m(
    one_minute: pd.DataFrame,
    daily: pd.DataFrame,
    frequency: str,
    *,
    dates: Sequence[str],
) -> pd.DataFrame:
    """Rebuild selected complete A-share intraday sessions from one-minute bars."""

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


def apply_repairs_once(
    dataframe: pd.DataFrame,
    series: SeriesKey,
    findings: Sequence[ValidationFinding],
    *,
    references: Mapping[str, pd.DataFrame] | None = None,
) -> tuple[pd.DataFrame, tuple[RepairRecord, ...]]:
    """Apply one deterministic repair transaction for the initial findings."""

    if not findings:
        return dataframe.copy(), ()
    references = references or {}
    result = dataframe.copy()
    records: list[RepairRecord] = []
    finding_codes = {item.code for item in findings}
    for binding in bindings_for(series):
        if not finding_codes.intersection(binding.finding_codes):
            continue
        if any(
            item.code in {"KNOWN_SOURCE_ANOMALY", "SOURCE_SIGNATURE_UNKNOWN"}
            and item.context.get("binding_id") not in {None, binding.binding_id}
            for item in findings
        ):
            continue
        before = frame_content_sha256(result)
        if binding.algorithm == "REPLACE_EXACT_FIELDS":
            repaired, affected = _replace_exact_fields(result, binding)
        elif binding.algorithm == "SCALE_VOLUME_ON_DATES":
            repaired, affected = _scale_volume_on_dates(result, binding, findings)
        elif binding.algorithm == "REBUILD_INTRADAY_FROM_1M":
            if "daily" not in references:
                raise DataRepairError(
                    f"{binding.binding_id}: 1m and daily repair references are required"
                )
            repair_dates = repair_dates_for(references["daily"], binding)
            if not repair_dates:
                continue
            if "1m" not in references:
                raise DataRepairError(
                    f"{binding.binding_id}: 1m and daily repair references are required"
                )
            rebuilt = rebuild_intraday_from_1m(
                references["1m"],
                references["daily"],
                series.frequency,
                dates=repair_dates,
            )
            current_dates = pd.to_datetime(result["Date"], errors="coerce").dt.strftime(
                "%Y-%m-%d"
            )
            repaired = pd.concat(
                [result.loc[~current_dates.isin(repair_dates)], rebuilt],
                ignore_index=True,
            )
            repaired["Date"] = pd.to_datetime(repaired["Date"], errors="raise")
            repaired = repaired.sort_values("Date").reset_index(drop=True)
            affected = repair_dates
        else:
            raise DataRepairError(f"unsupported repair algorithm {binding.algorithm}")
        if not affected:
            continue
        result = repaired
        records.append(
            RepairRecord(
                binding.binding_id,
                binding.algorithm,
                binding.algorithm_version,
                tuple(sorted(finding_codes.intersection(binding.finding_codes))),
                tuple(affected),
                before,
                frame_content_sha256(result),
            )
        )
    if not records:
        raise DataRepairError(
            f"no repair binding matched {series.vendor}/{series.symbol}/{series.frequency}",
            finding_codes=sorted(finding_codes),
        )
    return result, tuple(records)
