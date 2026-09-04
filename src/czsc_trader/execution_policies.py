"""Legacy advice type retained for callers of advice.v1/v2."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedExecutionPolicy:
    version: str
    status: str
    symbol: str
    baseline_version: str
    baseline_sha256: str
    family: str
    parameter: float
    atr_window: int
    tick: float
    fee_rate: float
    warning_gap_q05: float
    entry_order_type: str
    exit_limit_ratio: float
    exit_price_rounding: str
    exit_primary_order_type: str
    exit_continuous_fallback: str
    sha256: str
    source_path: str
    source_sha256: str
