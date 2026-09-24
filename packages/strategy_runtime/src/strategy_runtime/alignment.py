"""Causal cross-calendar input alignment for strategy implementations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

import pandas as pd

from .errors import RuntimeContractError


class AlignmentRule(StrEnum):
    """How source observations may be matched to decision timestamps."""

    EXACT = "EXACT"
    STRICT_PRIOR = "STRICT_PRIOR"
    LATEST_AVAILABLE = "LATEST_AVAILABLE"


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class InputAlignment:
    """Declare one auditable mapping from source time to decision time."""

    rule: AlignmentRule
    source_time_column: str
    source_calendar: str
    decision_calendar: str
    maximum_staleness_days: int | None
    allow_same_day: bool

    def __post_init__(self) -> None:
        if not isinstance(self.rule, AlignmentRule):
            raise RuntimeContractError("alignment rule must be an AlignmentRule")
        object.__setattr__(
            self, "source_time_column", _text(self.source_time_column, "source time column")
        )
        object.__setattr__(
            self, "source_calendar", _text(self.source_calendar, "source calendar")
        )
        object.__setattr__(
            self, "decision_calendar", _text(self.decision_calendar, "decision calendar")
        )
        if self.maximum_staleness_days is not None and (
            not isinstance(self.maximum_staleness_days, int)
            or isinstance(self.maximum_staleness_days, bool)
            or self.maximum_staleness_days < 0
        ):
            raise RuntimeContractError("maximum staleness days must be a non-negative integer")
        if not isinstance(self.allow_same_day, bool):
            raise RuntimeContractError("allow_same_day must be boolean")
        if self.rule is AlignmentRule.STRICT_PRIOR and self.allow_same_day:
            raise RuntimeContractError("STRICT_PRIOR cannot allow same-day observations")
        if self.rule is AlignmentRule.EXACT and not self.allow_same_day:
            raise RuntimeContractError("EXACT requires same-day observations")

    def identity_payload(self) -> dict[str, object]:
        """Return the JSON-safe form included in a runtime identity."""

        return {
            "rule": self.rule.value,
            "source_time_column": self.source_time_column,
            "source_calendar": self.source_calendar,
            "decision_calendar": self.decision_calendar,
            "maximum_staleness_days": self.maximum_staleness_days,
            "allow_same_day": self.allow_same_day,
        }


@dataclass(frozen=True, slots=True)
class AlignedInput:
    """Aligned values with their source timestamp and calendar-day age."""

    dataframe: pd.DataFrame
    alignment: InputAlignment

    def __post_init__(self) -> None:
        required = {"decision_time", "source_time", "staleness_days"}
        if not isinstance(self.dataframe, pd.DataFrame) or not required.issubset(
            self.dataframe.columns
        ):
            raise RuntimeContractError(
                "aligned input must contain decision_time, source_time and staleness_days"
            )
        object.__setattr__(self, "dataframe", self.dataframe.copy())


def _timestamps(values: Iterable[object], field_name: str) -> pd.Series:
    try:
        result = pd.Series(pd.to_datetime(list(values), errors="raise"), dtype="datetime64[ns]")
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"{field_name} contains invalid timestamps") from exc
    if result.empty or result.isna().any():
        raise RuntimeContractError(f"{field_name} must contain timestamps")
    if result.duplicated().any() or not result.is_monotonic_increasing:
        raise RuntimeContractError(f"{field_name} must be unique and ordered")
    return result


def align_input_history(
    source: pd.DataFrame,
    decision_times: Iterable[object],
    alignment: InputAlignment,
    *,
    value_columns: tuple[str, ...] | None = None,
) -> AlignedInput:
    """Align source observations without losing their original timestamps.

    Missing history remains missing so strategy warm-up is explicit. A matched
    observation older than the declared maximum is a contract failure.
    """

    if not isinstance(source, pd.DataFrame) or source.empty:
        raise RuntimeContractError("alignment source must be a non-empty dataframe")
    if not isinstance(alignment, InputAlignment):
        raise RuntimeContractError("alignment must be an InputAlignment")
    time_column = alignment.source_time_column
    if time_column not in source.columns:
        raise RuntimeContractError(f"alignment source has no {time_column} column")
    reserved = {"decision_time", "source_time", "staleness_days"}
    columns = (
        tuple(column for column in source.columns if column != time_column)
        if value_columns is None
        else tuple(value_columns)
    )
    if not columns or len(columns) != len(set(columns)):
        raise RuntimeContractError("alignment value columns must be unique and non-empty")
    missing = sorted(set(columns).difference(source.columns))
    if missing:
        raise RuntimeContractError(f"alignment value columns are missing: {missing}")
    if time_column in columns or reserved.intersection(columns):
        raise RuntimeContractError("alignment value columns conflict with audit columns")

    source_times = _timestamps(source[time_column], "source time")
    decisions = _timestamps(decision_times, "decision time")
    right = source.loc[:, list(columns)].copy()
    right.insert(0, "source_time", source_times.to_numpy())
    left = pd.DataFrame({"decision_time": decisions})
    right.insert(
        0,
        "_alignment_time",
        right["source_time"]
        if alignment.allow_same_day
        else right["source_time"].dt.normalize(),
    )
    left.insert(
        0,
        "_alignment_time",
        left["decision_time"]
        if alignment.allow_same_day
        else left["decision_time"].dt.normalize(),
    )

    if alignment.rule is AlignmentRule.EXACT:
        matched = left.merge(
            right,
            how="left",
            on="_alignment_time",
            sort=False,
            validate="one_to_one",
        )
    else:
        matched = pd.merge_asof(
            left,
            right,
            on="_alignment_time",
            direction="backward",
            allow_exact_matches=alignment.allow_same_day,
        )

    matched = matched.drop(columns="_alignment_time")
    age = (
        matched["decision_time"].dt.normalize()
        - matched["source_time"].dt.normalize()
    )
    matched.insert(2, "staleness_days", age.dt.days.astype("Int64"))
    if (matched["staleness_days"].dropna() < 0).any():
        raise RuntimeContractError("alignment selected a future source observation")
    limit = alignment.maximum_staleness_days
    if limit is not None:
        stale = matched["staleness_days"].dropna() > limit
        if stale.any():
            row = matched.loc[stale[stale].index[0]]
            raise RuntimeContractError(
                "aligned source exceeds maximum staleness: "
                f"decision_time={row['decision_time'].isoformat()}, "
                f"source_time={row['source_time'].isoformat()}, limit_days={limit}"
            )
    return AlignedInput(matched, alignment)
