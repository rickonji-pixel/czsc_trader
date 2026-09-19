from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from dataflows import DataIdentity, DataResult, DataStatus, Dataset
from strategy_manager import StrategyRegistry
from strategy_runtime import (
    DeploymentSpec,
    RuntimeContractError,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
    publish_history,
)


ROOT = Path(__file__).resolve().parents[4]


def _strategy(release_id: str):
    strategy_id, version = release_id.split("-", 1)
    stored = StrategyRegistry(ROOT / "strategies").get_version(strategy_id, version)
    return StrategyLoader().load(StrategyRelease.from_mapping(stored.to_dict()))


class RecordingDataflows:
    def __init__(self) -> None:
        self.requests = []

    def fetch(self, request):
        self.requests.append(request)
        dates = pd.bdate_range(request.start, request.end)
        if str(request.dataset) == Dataset.TRADING_CALENDAR.value:
            dates = pd.date_range(request.start, request.end)
            frame = pd.DataFrame({"Date": dates, "IsOpen": dates.weekday < 5})
        elif str(request.dataset) == Dataset.INDEX_CONSTITUENT_WEIGHT.value:
            frame = pd.DataFrame(
                {
                    "Date": dates,
                    "ConstituentSymbol": "000001.SZ",
                    "Weight": 1.0,
                }
            )
        elif str(request.dataset) == Dataset.STOCK_MONEYFLOW.value:
            session_dates = pd.to_datetime(request.options["trading_dates"])
            frame = pd.DataFrame(
                {
                    "Date": session_dates,
                    "Symbol": "000001.SZ",
                    "NetMoneyflowAmount": 1.0,
                }
            )
        else:
            frame = pd.DataFrame({"Date": dates, "Value": 1.0})
        identity = DataIdentity(
            str(request.dataset),
            "fixture",
            request.symbol,
            pd.Timestamp(frame["Date"].min()).isoformat(),
            pd.Timestamp(frame["Date"].max()).isoformat(),
            "a" * 64,
        )
        return DataResult(DataStatus.READY, frame, identity)


def _deployment(strategy, symbol: str) -> DeploymentSpec:
    definition = strategy.definition
    return DeploymentSpec(
        "history",
        definition.release_id,
        definition.release_hash,
        symbol,
        "backtest-data",
        "backtest",
        {"repository_root": str(ROOT)},
    )


@pytest.mark.parametrize(
    ("release_id", "symbol"),
    (("S001-v1", "588080.SH"), ("S001-v2", "588080.SH"), ("S002-v1", "510500.SH")),
)
def test_price_strategies_publish_every_declared_history_input(
    release_id: str,
    symbol: str,
) -> None:
    strategy = _strategy(release_id)
    publication = publish_history(
        strategy,
        RecordingDataflows(),
        _deployment(strategy, symbol),
        start=date(2020, 1, 1),
        through=date(2026, 9, 15),
    )

    StrategyRunner.validate_publication(strategy, publication)
    assert set(publication.input_results) == {
        item.name for item in strategy.definition.inputs.requirements
    }


def test_s007_history_uses_only_declared_market_inputs() -> None:
    strategy = _strategy("S007-v1")
    dataflows = RecordingDataflows()

    publication = publish_history(
        strategy,
        dataflows,
        _deployment(strategy, "588080.SH"),
        start=date(2020, 1, 1),
        through=date(2026, 9, 15),
    )

    StrategyRunner.validate_publication(strategy, publication)
    assert set(publication.input_results) == {
        item.name for item in strategy.definition.inputs.requirements
    }
    assert "strategy_evidence" not in publication.input_results
    share_request = publication.input_requests["etf_share_size"]
    assert share_request.end == "2026-09-14"
    assert share_request.required_cutoff == "2026-09-14"

    with pytest.raises(RuntimeContractError, match="symbol differs"):
        publish_history(
            strategy,
            dataflows,
            _deployment(strategy, "510500.SH"),
            start=date(2020, 1, 1),
            through=date(2026, 9, 15),
        )


def test_s007_history_policy_extends_a_short_research_window() -> None:
    strategy = _strategy("S007-v1")
    publication = publish_history(
        strategy,
        RecordingDataflows(),
        _deployment(strategy, "588080.SH"),
        start=date(2026, 6, 15),
        through=date(2026, 9, 17),
    )

    assert strategy.definition.history.mode == "CANONICAL_REPLAY"
    assert strategy.definition.history.canonical_start == "2021-01-04"
    assert strategy.definition.history.required_input_start == "2020-12-01"
    assert publication.input_requests["adjusted_daily"].start == "2020-12-01"


def test_s003_history_expands_cross_sectional_requests_from_contract() -> None:
    strategy = _strategy("S003-v1")
    dataflows = RecordingDataflows()

    publication = publish_history(
        strategy,
        dataflows,
        _deployment(strategy, "510500.SH"),
        start=date(2021, 1, 1),
        through=date(2021, 3, 31),
    )

    StrategyRunner.validate_publication(strategy, publication)
    weights = publication.input_requests["constituent_weights"]
    moneyflow = publication.input_requests["constituent_moneyflow"]
    assert weights.start == "2019-12-28"
    assert moneyflow.options["trading_dates"][0] == "2021-01-01"
    assert moneyflow.options["trading_dates"][-1] == "2021-03-31"
