"""Prepared data contracts and standard SRT data sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

import pandas as pd

from .contracts import StrategyIdentity, TradableWindow
from .errors import RuntimeContractError
from .models import ExecutionPricingData, PublishedStrategyData, RuntimeDefinition, canonical_sha256
from .publication_store import (
    _definition_symbol,
    _read_bound_generation,
    read_publication,
)
from .validation import validate_publication


@dataclass(frozen=True, slots=True)
class DataPreparationRequest:
    strategy: StrategyIdentity
    definition: RuntimeDefinition
    window: TradableWindow

    def __post_init__(self) -> None:
        if self.strategy.reference_id != self.definition.release_id:
            raise RuntimeContractError("data request strategy and definition IDs differ")
        if self.strategy.release_hash != self.definition.release_hash:
            raise RuntimeContractError("data request strategy and definition hashes differ")


@dataclass(frozen=True, slots=True)
class PreparedStrategyData:
    """Immutable-by-contract data admitted for one strategy tradable window."""

    strategy: StrategyIdentity
    window: TradableWindow
    available_through: date
    dataset_identity: str
    input_identities: Mapping[str, str]
    price_identities: Mapping[str, str]
    _tradable_dates: tuple[date, ...]
    _publication: PublishedStrategyData
    _pricing: ExecutionPricingData

    def __post_init__(self) -> None:
        if self.strategy.reference_id != self._publication.release_id:
            raise RuntimeContractError("prepared data release ID differs from strategy")
        if self.strategy.release_hash != self._publication.release_hash:
            raise RuntimeContractError("prepared data release hash differs from strategy")
        if self.strategy.symbol != self._pricing.symbol:
            raise RuntimeContractError("prepared data symbol differs from strategy")
        if not self._tradable_dates or tuple(sorted(set(self._tradable_dates))) != self._tradable_dates:
            raise RuntimeContractError("prepared tradable dates must be unique and ordered")
        if self.window.start not in self._tradable_dates or self.window.end not in self._tradable_dates:
            raise RuntimeContractError("prepared data does not cover the tradable window")
        inputs = MappingProxyType(dict(sorted(self.input_identities.items())))
        prices = MappingProxyType(dict(sorted(self.price_identities.items())))
        object.__setattr__(self, "input_identities", inputs)
        object.__setattr__(self, "price_identities", prices)
        expected = canonical_sha256(
            {
                "strategy": self.strategy.reference_id,
                "release_hash": self.strategy.release_hash,
                "window": {
                    "start": self.window.start.isoformat(),
                    "end": self.window.end.isoformat(),
                },
                "available_through": self.available_through.isoformat(),
                "tradable_dates": [value.isoformat() for value in self._tradable_dates],
                "inputs": dict(inputs),
                "prices": dict(prices),
            }
        )
        if self.dataset_identity != expected:
            raise RuntimeContractError("prepared dataset identity differs from admitted data")

    @classmethod
    def from_publication(
        cls,
        *,
        strategy: StrategyIdentity,
        window: TradableWindow,
        publication: PublishedStrategyData,
        pricing: ExecutionPricingData,
        tradable_dates: tuple[date, ...] | None = None,
    ) -> "PreparedStrategyData":
        input_identities = {
            name: result.identity.content_sha256
            for name, result in publication.input_results.items()
        }
        price_identities = dict(pricing.identity_hashes)
        available_through = date.fromisoformat(publication.requested_cutoff)
        if tradable_dates is None:
            calendars = [
                result.dataframe
                for result in publication.input_results.values()
                if {"Date", "IsOpen"} <= set(result.dataframe.columns)
            ]
            if not calendars:
                raise RuntimeContractError("prepared data has no trading calendar")
            frame = calendars[0]
            tradable_dates = tuple(
                dict.fromkeys(
                    pd.to_datetime(
                        frame.loc[frame["IsOpen"].astype(int).eq(1), "Date"],
                        errors="raise",
                    ).dt.date
                )
            )
        identity = canonical_sha256(
            {
                "strategy": strategy.reference_id,
                "release_hash": strategy.release_hash,
                "window": {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                },
                "available_through": available_through.isoformat(),
                "tradable_dates": [value.isoformat() for value in tradable_dates],
                "inputs": dict(sorted(input_identities.items())),
                "prices": dict(sorted(price_identities.items())),
            }
        )
        return cls(
            strategy,
            window,
            available_through,
            identity,
            input_identities,
            price_identities,
            tradable_dates,
            publication,
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
            return self._publication.input_results[name].dataframe.copy()
        except KeyError as exc:
            raise RuntimeContractError(f"prepared input is unavailable: {name}") from exc

    def _calendar_dates(self) -> tuple[date, ...]:
        return self._tradable_dates

    def trading_dates(self) -> tuple[date, ...]:
        return tuple(
            value
            for value in self._calendar_dates()
            if self.window.contains(value)
        )

    def signal_date_for(self, trading_date: date) -> date:
        if not self.window.contains(trading_date):
            raise RuntimeContractError("trading date is outside the strategy window")
        previous = [value for value in self._calendar_dates() if value < trading_date]
        if not previous:
            raise RuntimeContractError("prepared data has no signal session before trading date")
        return previous[-1]


@runtime_checkable
class StrategyDataSource(Protocol):
    def prepare(self, request: DataPreparationRequest) -> PreparedStrategyData: ...


class PublishedDataSource:
    """Read one authenticated SRT publication for a single decision cutoff."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory).resolve()

    def cutoff_for(self, reference_id: str) -> date:
        """Return the authenticated publication cutoff advertised for a strategy."""

        publication = read_publication(self._directory, reference_id)
        try:
            return date.fromisoformat(publication.requested_cutoff)
        except ValueError as exc:
            raise RuntimeContractError("strategy publication cutoff is invalid") from exc

    def trading_date_for(self, reference_id: str) -> date:
        publication = read_publication(self._directory, reference_id)
        cutoff = self.cutoff_for(reference_id)
        calendars = [
            result.dataframe
            for result in publication.input_results.values()
            if {"Date", "IsOpen"} <= set(result.dataframe.columns)
        ]
        if not calendars:
            raise RuntimeContractError("strategy publication has no trading calendar")
        frame = calendars[0]
        future = pd.to_datetime(
            frame.loc[frame["IsOpen"].astype(int).eq(1), "Date"], errors="raise"
        ).dt.date
        future = [value for value in future if value > cutoff]
        if not future:
            raise RuntimeContractError("strategy publication has no next trading session")
        return future[0]

    def prepare(self, request: DataPreparationRequest) -> PreparedStrategyData:
        if request.window.start != request.window.end:
            raise RuntimeContractError(
                "published data source supports one trading session per preparation"
            )
        symbol = _definition_symbol(request.definition)
        if symbol != request.strategy.symbol:
            raise RuntimeContractError("published data request symbol differs from strategy")
        generation = _read_bound_generation(
            self._directory,
            release_id=request.definition.release_id,
            symbol=symbol,
        )
        publication = read_publication(self._directory, request.definition.release_id)
        validate_publication(request.definition, publication)
        if publication.requested_cutoff != generation["data_cutoff"]:
            raise RuntimeContractError(
                "strategy publication cutoff differs from its SRT generation"
            )
        try:
            adjusted = publication.input_results["adjusted_daily"].dataframe
            execution = publication.input_results["execution_daily"].dataframe
        except KeyError as exc:
            raise RuntimeContractError(
                "SRT publication has no complete execution-pricing data"
            ) from exc
        prepared = PreparedStrategyData.from_publication(
            strategy=request.strategy,
            window=request.window,
            publication=publication,
            pricing=ExecutionPricingData(symbol, adjusted, execution),
        )
        if prepared.signal_date_for(request.window.start) != date.fromisoformat(
            publication.requested_cutoff
        ):
            raise RuntimeContractError(
                "strategy publication cutoff is not the signal session for trading"
            )
        return prepared


class HistoricalDataSource:
    """Admit a publication with caller-supplied historical execution prices."""

    def __init__(
        self,
        *,
        publication: PublishedStrategyData,
        symbol: str,
        adjusted_daily: pd.DataFrame,
        execution_daily: pd.DataFrame,
    ) -> None:
        self._publication = publication
        self._pricing = ExecutionPricingData(
            symbol.upper(), adjusted_daily, execution_daily
        )

    def prepare(self, request: DataPreparationRequest) -> PreparedStrategyData:
        validate_publication(request.definition, self._publication)
        if self._publication.release_id != request.strategy.reference_id:
            raise RuntimeContractError("historical publication belongs to another strategy")
        if self._pricing.symbol != request.strategy.symbol:
            raise RuntimeContractError("historical pricing belongs to another symbol")
        prepared = PreparedStrategyData.from_publication(
            strategy=request.strategy,
            window=request.window,
            publication=self._publication,
            pricing=self._pricing,
            tradable_dates=tuple(
                dict.fromkeys(
                    pd.to_datetime(
                        self._pricing.adjusted_daily["dt"], errors="raise"
                    ).dt.date
                )
            ),
        )
        cutoff = date.fromisoformat(self._publication.requested_cutoff)
        last_signal = prepared.signal_date_for(request.window.end)
        if cutoff < last_signal:
            raise RuntimeContractError("historical publication ends before the signal window")
        return prepared
