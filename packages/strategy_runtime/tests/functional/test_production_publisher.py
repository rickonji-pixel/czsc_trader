from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest
from dataflows import Dataflows, Dataset

from strategy_runtime.publisher import StrategyDataPublisher, StrategyPublicationError


def _flows(*, invalid_adjusted: bool = False):
    dates = pd.bdate_range(end="2026-09-02", periods=700)
    bars = pd.DataFrame(
        {
            "Date": dates,
            "Open": [6.0] * len(dates),
            "High": [6.1] * len(dates),
            "Low": [5.9] * len(dates),
            "Close": [6.0] * len(dates),
            "Volume": [1000.0] * len(dates),
            "Amount": [6000.0] * len(dates),
        }
    )

    def market(request):
        selected = bars.loc[
            pd.to_datetime(bars["Date"]).between(request.start, request.end)
        ].copy()
        if invalid_adjusted and "unadjusted" not in request.dataset:
            selected.loc[selected.index[-1], "High"] = 5.0
        return selected, {
            "vendor": "test",
            "adjustment": "none" if "unadjusted" in request.dataset else "hfq",
            "primary_key": ["Date"],
        }

    def calendar(request):
        days = pd.date_range(request.start, request.end)
        frame = pd.DataFrame(
            {"Date": days, "IsOpen": (days.dayofweek < 5).astype(int)}
        )
        return frame, {"vendor": "test", "primary_key": ["Date"]}

    def weights(request):
        selected = pd.bdate_range(request.start, request.end)
        return pd.DataFrame({
            "Date": selected,
            "ConstituentSymbol": ["000001.SZ"] * len(selected),
            "Weight": [1.0] * len(selected),
        }), {"vendor": "test", "primary_key": ["Date", "ConstituentSymbol"]}

    def moneyflow(request):
        selected = pd.to_datetime(request.options["trading_dates"])
        return pd.DataFrame({
            "Date": selected,
            "Symbol": ["000001.SZ"] * len(selected),
            "NetMoneyflowAmount": [1.0] * len(selected),
        }), {"vendor": "test", "primary_key": ["Date", "Symbol"]}

    return Dataflows(
        {
            Dataset.ETF_OHLCV.value: market,
            Dataset.ETF_UNADJUSTED_DAILY.value: market,
            Dataset.TRADING_CALENDAR.value: calendar,
            Dataset.INDEX_CONSTITUENT_WEIGHT.value: weights,
            Dataset.STOCK_MONEYFLOW.value: moneyflow,
        }
    )


@pytest.mark.parametrize("release_id", ["S002-v1", "S003-v1"])
def test_srt_publisher_commits_authenticated_generation(tmp_path, release_id):
    root = Path(__file__).resolve().parents[4]
    data_dir = tmp_path / "data"
    strategy_id, version = release_id.split("-", 1)
    result = StrategyDataPublisher(
        repo_root=root, data_dir=data_dir, dataflows=_flows()
    ).publish_release(
        "510500.SH", "etf", [(strategy_id, version)], "2026-09-02"
    )

    assert result["publisher"] == "SRT_DFLS"
    assert result["strategy_releases"] == [release_id]
    assert (data_dir / f"srt_{release_id.lower().replace('-', '_')}_publication.json").is_file()
    assert (data_dir / "510500_manifest.json").is_file()
    generation = json.loads(
        (data_dir / "510500_strategy_generation.json").read_text(encoding="utf-8")
    )
    assert generation["generation_id"] == result["generation_id"]
    validation = json.loads(
        (data_dir / "510500_validation.json").read_text(encoding="utf-8")
    )
    assert validation["status"] == "PASS"
    assert validation["contract"] == "srt.market-compatibility.v1"
    assert not list(tmp_path.glob(".srt-publication-*"))


def test_publication_target_fails_closed_on_incomplete_calendar(tmp_path):
    def calendar(request):
        days = pd.date_range(request.start, request.end)[1:]
        return pd.DataFrame(
            {"Date": days, "IsOpen": (days.dayofweek < 5).astype(int)}
        ), {}

    publisher = StrategyDataPublisher(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        dataflows=Dataflows({Dataset.TRADING_CALENDAR.value: calendar}),
    )
    with pytest.raises(StrategyPublicationError, match="does not cover every requested"):
        publisher.publication_target(date(2026, 9, 19))


def test_srt_publisher_rejects_unsafe_identity_before_writing(tmp_path):
    with pytest.raises(StrategyPublicationError, match="A-share"):
        StrategyDataPublisher(
            repo_root=tmp_path, data_dir=tmp_path / "data", dataflows=_flows()
        ).publish_release("../588080.SH", "etf", [("S007", "v1")], "2026-09-02")
    assert not (tmp_path / "data").exists()


def test_srt_publisher_blocks_failed_dfls_validation(tmp_path):
    root = Path(__file__).resolve().parents[4]
    data_dir = tmp_path / "data"

    with pytest.raises(StrategyPublicationError, match="publication is FAILED"):
        StrategyDataPublisher(
            repo_root=root,
            data_dir=data_dir,
            dataflows=_flows(invalid_adjusted=True),
        ).publish_release("510500.SH", "etf", [("S002", "v1")], "2026-09-02")

    assert not data_dir.exists()
