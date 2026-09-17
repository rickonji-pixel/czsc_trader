from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import (
    UpdateBacktestDataCommand,
    _assert_append_only,
    _initial_backtest_start,
    update_backtest_data,
)


ROOT = Path(__file__).resolve().parents[2]


def test_cross_sectional_history_is_append_only_by_date_and_symbol(tmp_path: Path) -> None:
    current = tmp_path / "current.csv"
    proposed = tmp_path / "proposed.csv"
    current.write_text(
        "Date,MemberCode,Value\n2026-09-14,000001.SZ,1\n2026-09-14,000002.SZ,2\n",
        encoding="utf-8",
    )
    proposed.write_text(
        "Date,MemberCode,Value\n"
        "2026-09-14,000001.SZ,1\n"
        "2026-09-14,000002.SZ,2\n"
        "2026-09-15,000001.SZ,3\n",
        encoding="utf-8",
    )
    _assert_append_only(current, proposed, primary_key=("Date", "MemberCode"))

    proposed.write_text(
        "Date,MemberCode,Value\n2026-09-14,000001.SZ,9\n2026-09-14,000002.SZ,2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mutate published rows"):
        _assert_append_only(current, proposed, primary_key=("Date", "MemberCode"))


def _context(root: Path) -> RepositoryContext:
    return RepositoryContext(
        root=root,
        raw_dir=root / "data" / "raw",
        research_data_root=root / "data" / "raw",
        backtest_data_root=root / "data" / "backtest",
        baseline_root=root / "strategies" / "dependencies" / "legacy_rule_baselines",
        strategy_root=root / "strategies",
        strategy_dependency_root=root / "strategies" / "dependencies" / "legacy_rule_baselines",
        experiments_root=root / "experiments",
        outputs_root=root / "outputs",
    )


def test_ft_t01_initial_backtest_publication_inherits_research_data_start(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.research_data_root.mkdir(parents=True)
    (context.research_data_root / "510500_manifest.json").write_text(
        json.dumps({"requested_start": "2019-10-08"}), encoding="utf-8"
    )

    assert _initial_backtest_start(context, "510500") == date(2019, 10, 8)

    (context.research_data_root / "510500_manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="requested_start"):
        _initial_backtest_start(context, "510500")


def test_ft_t02_etf_backtest_publication_includes_intraday_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    object.__setattr__(context, "strategy_root", ROOT / "strategies")
    context.research_data_root.mkdir(parents=True)
    (context.research_data_root / "510500_manifest.json").write_text(
        json.dumps({"requested_start": "2019-10-08"}), encoding="utf-8"
    )
    calls: list[tuple[str, date, date]] = []

    def fake_market_data(symbol, asset_type, start, through, staging, **_kwargs):
        assert (symbol, asset_type) == ("510500.SH", "etf")
        (staging / "510500_manifest.json").write_text("{}", encoding="utf-8")
        return {"manifest": "market-manifest"}

    def fake_intraday_data(symbol, start, through, staging, **_kwargs):
        calls.append((symbol, start, through))
        (staging / "510500_intraday_manifest.json").write_text("{}", encoding="utf-8")
        return {"manifest": "intraday-manifest"}

    def fake_runtime_history(_context, *, strategy, staging, through, **_kwargs):
        (staging / "srt_s003_v1_publication.json").write_text(
            json.dumps({"inputs": {}}), encoding="utf-8"
        )
        return {
            "status": "READY",
            "release_id": strategy.definition.release_id,
            "requested_cutoff": through.isoformat(),
        }

    monkeypatch.setattr("czsc_trader.market_data_prep.prepare_market_data", fake_market_data)
    monkeypatch.setattr(
        "czsc_trader.intraday_data.prepare_intraday_research_data", fake_intraday_data
    )
    monkeypatch.setattr(
        "czsc_trader.application.data_service._publish_runtime_history",
        fake_runtime_history,
    )

    result = update_backtest_data(
        context,
        UpdateBacktestDataCommand("510500.SH", "etf", date(2026, 9, 15), "S003", "v1"),
    )

    assert calls == [("510500.SH", date(2019, 10, 8), date(2026, 9, 15))]
    assert result.result["data_contract"]["source"] == "SRT"
    assert result.result["data_contract"]["execution_intraday_frequencies"] == ["5m"]
    assert result.result["intraday"] == {"manifest": "intraday-manifest"}
    assert result.result["runtime_publication"]["release_id"] == "S003-v1"
    assert (context.backtest_data_root / "510500_intraday_manifest.json").is_file()


def test_ft_t03_backtest_publication_uses_srt_inputs_without_legacy_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    object.__setattr__(context, "strategy_root", ROOT / "strategies")
    context.research_data_root.mkdir(parents=True)
    (context.research_data_root / "588080_manifest.json").write_text(
        json.dumps({"requested_start": "2020-01-01"}), encoding="utf-8"
    )

    def fake_market_data(_symbol, _asset, _start, _through, staging, **_kwargs):
        (staging / "588080_manifest.json").write_text("{}", encoding="utf-8")
        return {"manifest": "market-manifest"}

    def fake_runtime_history(_context, *, strategy, staging, through, **_kwargs):
        (staging / "srt_s007_v1_adjusted_daily.csv.gz").write_text(
            "Date,Close\n2026-09-15,1\n", encoding="utf-8"
        )
        (staging / "srt_s007_v1_publication.json").write_text(
            json.dumps({"inputs": {}}), encoding="utf-8"
        )
        assert strategy.definition.release_id == "S007-v1"
        return {
            "status": "READY",
            "release_id": "S007-v1",
            "requested_cutoff": through.isoformat(),
            "inputs": {"adjusted_daily": {"dataset": "etf.ohlcv"}},
        }

    monkeypatch.setattr("czsc_trader.market_data_prep.prepare_market_data", fake_market_data)
    monkeypatch.setattr(
        "czsc_trader.application.data_service._publish_runtime_history",
        fake_runtime_history,
    )

    result = update_backtest_data(
        context,
        UpdateBacktestDataCommand("588080.SH", "etf", date(2026, 9, 15), "S007", "v1"),
    )

    assert result.result["data_contract"]["source"] == "SRT"
    assert len(result.result["data_contract"]["inputs"]) == 7
    assert result.result["runtime_publication"]["inputs"] == {
        "adjusted_daily": {"dataset": "etf.ohlcv"}
    }
    assert (context.backtest_data_root / "srt_s007_v1_publication.json").is_file()
