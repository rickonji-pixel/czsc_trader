from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import (
    UpdateBacktestDataCommand,
    _assert_append_only,
    _commit_generation,
    _initial_backtest_start,
    _verify_committed_generation,
    update_backtest_data,
)
from czsc_trader.generation_integrity import file_sha256, validate_strategy_generation


ROOT = Path(__file__).resolve().parents[2]


def test_generation_marker_rejects_modified_bound_file(tmp_path: Path) -> None:
    data = tmp_path / "588080_daily_2026.csv"
    data.write_text("date,close\n2026-01-05,1\n", encoding="utf-8")
    marker = {
        "schema_version": 1,
        "generation_id": "GEN-TEST",
        "dataset": "backtest",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "data_cutoff": "2026-01-05",
        "strategy_releases": ["S007-v1"],
        "files": {data.name: file_sha256(data)},
    }
    (tmp_path / "588080_strategy_generation.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    validate_strategy_generation(
        tmp_path,
        symbol="588080.SH",
        asset_type="etf",
        dataset="backtest",
    )

    data.write_text("date,close\n2026-01-05,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        validate_strategy_generation(
            tmp_path,
            symbol="588080.SH",
            asset_type="etf",
            dataset="backtest",
        )


def test_generation_commit_rolls_back_when_post_publish_verification_fails(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    target.mkdir()
    staging.mkdir()
    current = target / "588080_manifest.json"
    proposed = staging / current.name
    current.write_text("old", encoding="utf-8")
    proposed.write_text("new", encoding="utf-8")

    with pytest.raises(ValueError, match="post-publish verification failed"):
        _commit_generation(
            staging,
            target,
            [proposed],
            append_only=False,
            code="588080",
            verify=lambda: (_ for _ in ()).throw(
                ValueError("post-publish verification failed")
            ),
        )

    assert current.read_text(encoding="utf-8") == "old"


def test_committed_generation_is_reloaded_through_market_contracts(
    functional_repo: Path,
) -> None:
    _verify_committed_generation(
        functional_repo / "data" / "backtest",
        symbol="588080.SH",
        asset_type="etf",
        dataset="backtest",
    )


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
        strategy_root=root / "strategies",
        experiments_root=root / "experiments",
        outputs_root=root / "outputs",
    )


def test_ft_t01_initial_backtest_preparation_inherits_research_data_start(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.research_data_root.mkdir(parents=True)
    (context.research_data_root / "510500_manifest.json").write_text(
        json.dumps({"requested_start": "2019-10-08"}), encoding="utf-8"
    )

    assert _initial_backtest_start(context, "510500") == date(2019, 10, 8)

    (context.research_data_root / "510500_manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="requested_start"):
        _initial_backtest_start(context, "510500")


def test_ft_t02_etf_backtest_preparation_includes_intraday_data(
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
        return {"manifest": "market-manifest", "data_cutoff": through.isoformat()}

    def fake_intraday_data(symbol, start, through, staging, **_kwargs):
        calls.append((symbol, start, through))
        (staging / "510500_intraday_manifest.json").write_text("{}", encoding="utf-8")
        return {"manifest": "intraday-manifest"}

    monkeypatch.setattr("czsc_trader.market_data_prep.prepare_market_data", fake_market_data)
    monkeypatch.setattr(
        "czsc_trader.intraday_data.prepare_intraday_research_data", fake_intraday_data
    )
    monkeypatch.setattr(
        "czsc_trader.application.data_service._verify_committed_generation",
        lambda *_args, **_kwargs: None,
    )

    result = update_backtest_data(
        context,
        UpdateBacktestDataCommand("510500.SH", "etf", date(2026, 9, 15), "S003", "v1"),
    )

    assert calls == [("510500.SH", date(2019, 10, 8), date(2026, 9, 15))]
    assert result.result["data_contract"]["source"] == "SRT"
    assert result.result["data_contract"]["execution_intraday_frequencies"] == ["5m"]
    assert result.result["intraday"] == {"manifest": "intraday-manifest"}
    assert (context.backtest_data_root / "510500_intraday_manifest.json").is_file()


def test_ft_t03_backtest_preparation_describes_srt_contract_without_data_coupling(
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
        return {"manifest": "market-manifest", "data_cutoff": _through.isoformat()}

    monkeypatch.setattr("czsc_trader.market_data_prep.prepare_market_data", fake_market_data)
    monkeypatch.setattr(
        "czsc_trader.application.data_service._verify_committed_generation",
        lambda *_args, **_kwargs: None,
    )

    result = update_backtest_data(
        context,
        UpdateBacktestDataCommand("588080.SH", "etf", date(2026, 9, 15), "S007", "v1"),
    )

    assert result.result["data_contract"]["source"] == "SRT"
    assert len(result.result["data_contract"]["inputs"]) == 7
    assert not any(context.backtest_data_root.glob("srt_*"))
