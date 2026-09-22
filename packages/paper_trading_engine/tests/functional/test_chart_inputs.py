from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest
from dataflows import Dataset

from paper_trading_engine.chart_market_data import AccountChartMarketData
from paper_trading_engine.chart_strategy_metadata import AccountChartStrategyMetadata


def test_chart_market_data_reads_adjusted_bars_without_strategy_instance() -> None:
    requests = []
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-09-01", "2026-09-02"]),
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
        }
    )

    def fetch(request):
        requests.append(request)
        return SimpleNamespace(ready=True, dataframe=frame, error=None, status="READY")

    source = AccountChartMarketData(
        dataflows=SimpleNamespace(fetch=fetch),
        today=lambda: pd.Timestamp("2026-09-22").date(),
    )

    identity, actual = source.history(
        symbol="588080.SH",
        asset="etf",
        selection_data_cutoff="2026-09-02",
        context_sessions=60,
    )

    assert len(identity) == 64
    assert actual.equals(frame)
    assert len(requests) == 1
    request = requests[0]
    assert request.dataset == Dataset.ETF_OHLCV
    assert request.symbol == "588080.SH"
    assert request.end == "2026-09-22"
    assert request.required_cutoff == "2026-09-22"


def test_chart_strategy_metadata_is_bound_to_frozen_release(tmp_path) -> None:
    version = tmp_path / "strategies" / "S007" / "versions" / "v1.json"
    version.parent.mkdir(parents=True)
    version.write_text(
        json.dumps(
            {
                "release_hash": "a" * 64,
                "strategy_payload": {"rule": {"entry": 0.2}},
            }
        ),
        encoding="utf-8",
    )
    metadata = AccountChartStrategyMetadata(tmp_path)

    assert metadata.configuration(
        strategy_id="S007", strategy_version="v1", release_hash="a" * 64
    ) == {"rule": {"entry": 0.2}}
    with pytest.raises(ValueError, match="identity differs"):
        metadata.configuration(
            strategy_id="S007", strategy_version="v1", release_hash="b" * 64
        )
