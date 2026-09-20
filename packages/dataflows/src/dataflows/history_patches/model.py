"""Shared data structures for source-bound history repair patches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..history_validation import ValidationFinding


@dataclass(frozen=True, slots=True)
class SeriesKey:
    vendor: str
    endpoint: str
    symbol: str
    dataset: str
    frequency: str
    adjustment: str


@dataclass(frozen=True, slots=True)
class RepairPatch:
    """All repairs owned by one exact vendor-symbol pair."""

    patch_id: str
    patch_version: int
    vendor: str
    symbol: str
    execute: Callable[..., tuple[pd.DataFrame, tuple[str, ...]]] = field(
        compare=False, repr=False
    )
    inspect: Callable[..., tuple[ValidationFinding, ...]] | None = field(
        default=None, compare=False, repr=False
    )
    reference_dates: Callable[..., tuple[str, ...]] | None = field(
        default=None, compare=False, repr=False
    )

    def matches(self, series: SeriesKey) -> bool:
        return self.vendor == series.vendor and self.symbol == series.symbol


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """Immutable evidence produced by one actual repair-patch execution."""

    patch_id: str
    patch_version: int
    findings_before: tuple[str, ...]
    affected_dates: tuple[str, ...]
    raw_content_sha256: str
    repaired_content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "patch_id": self.patch_id,
            "patch_version": self.patch_version,
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
