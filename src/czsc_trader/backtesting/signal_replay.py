from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from strategy_runtime import StrategyRuntimeContext

from .models import StrategySnapshot


@dataclass(frozen=True)
class SignalReplay:
    snapshot: StrategySnapshot
    decisions: pd.DataFrame
    calculation_start: pd.Timestamp
    calculation_end: pd.Timestamp
    evaluation_start: pd.Timestamp
    evaluation_end: pd.Timestamp
    runtime_context: StrategyRuntimeContext
    support_data: dict[str, object] | None = None
    chart_data: pd.DataFrame | None = None
