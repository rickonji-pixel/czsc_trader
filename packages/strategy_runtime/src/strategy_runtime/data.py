"""Prepared data contracts and standard SRT data sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from .contracts import StrategyIdentity, TradableWindow
from .errors import RuntimeContractError
from .models import ExecutionPricingData, canonical_sha256
from .preparation import PreparedInputs


@dataclass(frozen=True, slots=True)
class PreparedStrategyData:
    """Immutable-by-contract data admitted for one strategy tradable window."""

    strategy: StrategyIdentity
    tradable_window: TradableWindow
    available_through: date
    dataset_identity: str
    input_identities: Mapping[str, str]
    price_identities: Mapping[str, str]
    _calendar_dates: tuple[date, ...]
    _calculation_dates: tuple[date, ...]
    _inputs: PreparedInputs
    _pricing: ExecutionPricingData

    def __post_init__(self) -> None:
        if self.strategy != self._inputs.strategy:
            raise RuntimeContractError("prepared inputs belong to another strategy")
        if self.strategy.symbol != self._pricing.symbol:
            raise RuntimeContractError("prepared data symbol differs from strategy")
        if (
            not self._calendar_dates
            or tuple(sorted(set(self._calendar_dates))) != self._calendar_dates
        ):
            raise RuntimeContractError("prepared calendar dates must be unique and ordered")
        if (
            self.tradable_window.start not in self._calendar_dates
            or self.tradable_window.end not in self._calendar_dates
        ):
            raise RuntimeContractError("prepared data does not cover the tradable window")
        if (
            not self._calculation_dates
            or self._calculation_dates[-1] != self.available_through
        ):
            raise RuntimeContractError("prepared calculation dates have an invalid cutoff")
        inputs = MappingProxyType(dict(sorted(self.input_identities.items())))
        prices = MappingProxyType(dict(sorted(self.price_identities.items())))
        object.__setattr__(self, "input_identities", inputs)
        object.__setattr__(self, "price_identities", prices)
        expected = canonical_sha256(
            {
                "strategy": self.strategy.reference_id,
                "release_hash": self.strategy.release_hash,
                "tradable_window": {
                    "start": self.tradable_window.start.isoformat(),
                    "end": self.tradable_window.end.isoformat(),
                },
                "available_through": self.available_through.isoformat(),
                "calendar_dates": [value.isoformat() for value in self._calendar_dates],
                "calculation_dates": [
                    value.isoformat() for value in self._calculation_dates
                ],
                "inputs": dict(inputs),
                "prices": dict(prices),
            }
        )
        if self.dataset_identity != expected:
            raise RuntimeContractError("prepared dataset identity differs from admitted data")

    @classmethod
    def from_inputs(
        cls,
        *,
        inputs: PreparedInputs,
        pricing: ExecutionPricingData,
    ) -> "PreparedStrategyData":
        input_identities = {
            name: result.identity.content_sha256
            for name, result in inputs.results.items()
        }
        price_identities = dict(pricing.identity_hashes)
        identity = canonical_sha256(
            {
                "strategy": inputs.strategy.reference_id,
                "release_hash": inputs.strategy.release_hash,
                "tradable_window": {
                    "start": inputs.tradable_window.start.isoformat(),
                    "end": inputs.tradable_window.end.isoformat(),
                },
                "available_through": inputs.available_through.isoformat(),
                "calendar_dates": [
                    value.isoformat() for value in inputs.calendar_dates
                ],
                "calculation_dates": [
                    value.isoformat() for value in inputs.calculation_dates
                ],
                "inputs": dict(sorted(input_identities.items())),
                "prices": dict(sorted(price_identities.items())),
            }
        )
        return cls(
            inputs.strategy,
            inputs.tradable_window,
            inputs.available_through,
            identity,
            input_identities,
            price_identities,
            inputs.calendar_dates,
            inputs.calculation_dates,
            inputs,
            pricing,
        )

    @property
    def adjusted_daily(self) -> pd.DataFrame:
        return self._pricing.adjusted_daily.copy()

    @property
    def execution_daily(self) -> pd.DataFrame:
        return self._pricing.execution_daily.copy()

    def input_frame(self, name: str) -> pd.DataFrame:
        try:
            return self._inputs.results[name].dataframe.copy()
        except KeyError as exc:
            raise RuntimeContractError(f"prepared input is unavailable: {name}") from exc

    def calendar_dates(self) -> tuple[date, ...]:
        return self._calendar_dates

    def calculation_dates(self) -> tuple[date, ...]:
        return self._calculation_dates

    def trading_dates(self) -> tuple[date, ...]:
        return tuple(
            value
            for value in self._calendar_dates
            if self.tradable_window.contains(value)
        )

    def signal_date_for(self, trading_date: date) -> date:
        if not self.tradable_window.contains(trading_date):
            raise RuntimeContractError("trading date is outside the strategy window")
        previous = [value for value in self._calendar_dates if value < trading_date]
        if not previous:
            raise RuntimeContractError("prepared data has no signal session before trading date")
        return previous[-1]
