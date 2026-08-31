"""Exact two-value representation for the CZSC BI power-layer signal."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class JointFactors:
    factors: pd.DataFrame
    directions: pd.DataFrame


def parse_signal_values(value: object) -> tuple[str | None, str | None]:
    """Return v1 and v2 from a canonical ``v1_v2_v3_score`` value."""
    if value is None or pd.isna(value):
        return None, None
    parts = str(value).split("_")
    if len(parts) < 2:
        raise ValueError(f"CZSC signal value has fewer than two fields: {value!r}")
    return parts[0], parts[1]


def build_joint_factors(
    raw: pd.Series,
    directions: Sequence[str],
    layers: Sequence[str],
) -> JointFactors:
    """Build the exact Cartesian direction-by-layer one-hot states."""
    parsed = pd.DataFrame(
        [parse_signal_values(value) for value in raw],
        index=raw.index,
        columns=["direction", "layer"],
    )
    direction_frame = pd.DataFrame(
        {str(direction): parsed["direction"].eq(str(direction)) for direction in directions},
        index=raw.index,
    )
    factors: dict[str, pd.Series] = {}
    for direction in map(str, directions):
        for layer in map(str, layers):
            name = f"joint__cxt_bi_zdf::{direction}::{layer}"
            factors[name] = (
                parsed["direction"].eq(direction) & parsed["layer"].eq(layer)
            ).astype(float)
    return JointFactors(pd.DataFrame(factors, index=raw.index), direction_frame)
