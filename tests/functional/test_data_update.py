from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import (
    UpdateBacktestDataCommand,
    _initial_backtest_start,
    update_backtest_data,
)
from czsc_trader.backtesting import BacktestDataContract


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

    monkeypatch.setattr("czsc_trader.market_data_prep.prepare_market_data", fake_market_data)
    monkeypatch.setattr(
        "czsc_trader.intraday_data.prepare_intraday_research_data", fake_intraday_data
    )
    monkeypatch.setattr(
        "czsc_trader.backtesting.resolve_registered_strategy",
        lambda *_args: SimpleNamespace(identity=SimpleNamespace(reference="S003-v1")),
    )
    monkeypatch.setattr(
        "czsc_trader.backtesting.resolve_backtest_data_contract",
        lambda _snapshot: BacktestDataContract(intraday_frequencies=("5m",)),
    )

    result = update_backtest_data(
        context,
        UpdateBacktestDataCommand("510500.SH", "etf", date(2026, 9, 15), "S003", "v1"),
    )

    assert calls == [("510500.SH", date(2019, 10, 8), date(2026, 9, 15))]
    assert result.result["data_contract"]["intraday_frequencies"] == ["5m"]
    assert result.result["intraday"] == {"manifest": "intraday-manifest"}
    assert (context.backtest_data_root / "510500_intraday_manifest.json").is_file()


def test_ft_t03_backtest_publication_includes_causal_feature_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    context.research_data_root.mkdir(parents=True)
    (context.research_data_root / "588080_manifest.json").write_text(
        json.dumps({"requested_start": "2020-01-01"}), encoding="utf-8"
    )

    def fake_market_data(_symbol, _asset, _start, _through, staging, **_kwargs):
        (staging / "588080_manifest.json").write_text("{}", encoding="utf-8")
        return {"manifest": "market-manifest"}

    def fake_support(_root, staging, release_id, _spec, _through):
        (staging / "s007_v1_causal_feature_panel.csv.gz").write_text(
            "date,value\n2026-09-15,1\n", encoding="utf-8"
        )
        (staging / "s007_v1_causal_feature_manifest.json").write_text("{}", encoding="utf-8")
        assert release_id == "S007-v1"
        return {"support_last_session": "2026-09-15"}

    monkeypatch.setattr("czsc_trader.market_data_prep.prepare_market_data", fake_market_data)
    monkeypatch.setattr(
        "czsc_trader.causal_feature_gate_runtime.publish_support_data", fake_support
    )
    monkeypatch.setattr(
        "czsc_trader.backtesting.resolve_registered_strategy",
        lambda *_args: SimpleNamespace(
            identity=SimpleNamespace(reference="S007-v1"),
            resolved_rule=SimpleNamespace(causal_feature_gate=object()),
        ),
    )
    monkeypatch.setattr(
        "czsc_trader.backtesting.resolve_backtest_data_contract",
        lambda _snapshot: BacktestDataContract(strategy_support=("causal_feature_panel",)),
    )

    result = update_backtest_data(
        context,
        UpdateBacktestDataCommand("588080.SH", "etf", date(2026, 9, 15), "S007", "v1"),
    )

    assert result.result["strategy_support"] == {
        "support_last_session": "2026-09-15",
        "data_cutoff": "2026-09-15",
        "support_type": "causal_feature",
    }
    assert (context.backtest_data_root / "s007_v1_causal_feature_panel.csv.gz").is_file()
