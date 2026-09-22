"""Required implementation surface for every strategy algorithm."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Mapping

import pandas as pd

from .calculation import CalculationScope, CalendarWindow
from .contracts import TradableWindow
from .models import RuntimeDefinition


class StrategyImplementation(ABC):
    """Base class that makes every strategy-owned responsibility explicit."""

    @property
    @abstractmethod
    def definition(self) -> RuntimeDefinition:
        """Return the immutable identity, inputs and execution contracts."""

        raise NotImplementedError

    @abstractmethod
    def calendar_window(self, tradable_window: TradableWindow) -> CalendarWindow:
        """Return the calendar range needed to resolve the tradable window."""

        raise NotImplementedError

    @abstractmethod
    def derive_calculation_scope(
        self,
        tradable_window: TradableWindow,
        calendar_dates: tuple[date, ...],
    ) -> CalculationScope:
        """Derive signal dates and the exact range of every strategy input."""

        raise NotImplementedError

    @abstractmethod
    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        """Calculate the channel-neutral strategy history for prepared inputs."""

        raise NotImplementedError

    def calculate_window_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        """Calculate an evaluation window with strategy state reset at its left edge."""

        return self.calculate_history(inputs, sessions)
