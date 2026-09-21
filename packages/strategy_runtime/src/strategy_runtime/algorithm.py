"""Internal contract implemented by a loaded strategy algorithm."""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

import pandas as pd

from .models import RuntimeDefinition


@runtime_checkable
class StrategyAlgorithm(Protocol):
    @property
    def definition(self) -> RuntimeDefinition: ...

    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame: ...
