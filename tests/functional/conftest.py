from __future__ import annotations

from pathlib import Path
import shutil
import json
import importlib
import sys

import strategy_runtime.strategies
from strategy_runtime import implementation_identity

import pandas as pd
import pytest
from dataflows import Dataflows
from czsc_trader.generation_integrity import file_sha256


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def candidate_payload(tmp_path, monkeypatch):
    package = tmp_path / "runtime"
    strategies = package / "strategies"
    strategies.mkdir(parents=True)
    source = Path(__file__).parents[1] / "fixtures" / "candidate_runtime.py"
    shutil.copyfile(source, strategies / "candidate_fixture.py")
    module_name = "strategy_runtime.strategies.candidate_fixture"
    monkeypatch.setattr(strategy_runtime.strategies, "__path__", [str(strategies)])
    monkeypatch.setattr(implementation_identity, "files", lambda _: package)
    importlib.invalidate_caches()
    payload = {
        "runtime": {
            "module": module_name,
            "qualname": "CandidateFixture",
            "contract_version": 1,
            "source_files": ["strategies/candidate_fixture.py"],
            "source_sha256": implementation_identity.implementation_sha256(
                ("strategies/candidate_fixture.py",),
            ),
        },
        "parameters": {"threshold": 0.5},
    }
    yield payload, package
    sys.modules.pop(module_name, None)


def _frame(root: Path, pattern: str, date_column: str) -> pd.DataFrame:
    parts = [pd.read_csv(path) for path in sorted(root.glob(pattern))]
    frame = pd.concat(parts, ignore_index=True)
    return frame.rename(
        columns={
            date_column: "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "amount": "Amount",
        }
    )


def _publish_s001_fixture(root: Path) -> None:
    data_root = root / "data" / "backtest"
    frames = {
        "adjusted_30m": _frame(data_root, "588080_30m_*.csv", "datetime"),
        "adjusted_daily": _frame(data_root, "588080_daily_*.csv", "date"),
        "adjusted_weekly": _frame(data_root, "588080_weekly_*.csv", "date"),
        "execution_daily": _frame(data_root, "588080_execution_daily_*.csv", "date"),
    }
    cutoff = pd.Timestamp(frames["adjusted_daily"]["Date"].max()).date()
    calendar_end = pd.Timestamp(cutoff) + pd.Timedelta(days=20)
    dates = pd.date_range(frames["adjusted_daily"]["Date"].min(), calendar_end)
    frames["trading_calendar"] = pd.DataFrame(
        {"Date": dates, "IsOpen": dates.weekday < 5}
    )
    files = {
        path.name: file_sha256(path)
        for path in data_root.iterdir()
        if path.is_file() and path.name != "588080_strategy_generation.json"
    }
    (data_root / "588080_strategy_generation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation_id": "GEN-FUNCTIONAL-S001",
                "dataset": "backtest",
                "symbol": "588080.SH",
                "asset_type": "etf",
                "data_cutoff": cutoff.isoformat(),
                "strategy_releases": ["S001-v1"],
                "data_contracts": [],
                "runtime_publications": [],
                "files": files,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def functional_repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "czsc_trader").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='czsc-trader-functional-test'\nversion='0.1.0'\n",
        encoding="utf-8",
    )
    shutil.copytree(REPO_ROOT / "strategies", root / "strategies")
    raw_dir = root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for source in (REPO_ROOT / "data" / "raw").glob("588080*"):
        shutil.copy2(source, raw_dir / source.name)
    shutil.copytree(raw_dir, root / "data" / "backtest")
    for source in (REPO_ROOT / "data" / "backtest").glob("s007_v1_causal_feature_*"):
        shutil.copy2(source, root / "data" / "backtest" / source.name)
    _publish_s001_fixture(root)
    frames = {
        ("etf.ohlcv", "588080.SH", "30m"): _frame(
            root / "data" / "backtest", "588080_30m_*.csv", "datetime"
        ),
        ("etf.ohlcv", "588080.SH", "daily"): _frame(
            root / "data" / "backtest", "588080_daily_*.csv", "date"
        ),
        ("etf.ohlcv", "588080.SH", "weekly"): _frame(
            root / "data" / "backtest", "588080_weekly_*.csv", "date"
        ),
        ("etf.unadjusted_daily", "588080.SH", "daily"): _frame(
            root / "data" / "backtest", "588080_execution_daily_*.csv", "date"
        ),
    }
    calendar_end = pd.Timestamp(frames[("etf.ohlcv", "588080.SH", "daily")]["Date"].max()) + pd.Timedelta(days=20)
    dates = pd.date_range(
        frames[("etf.ohlcv", "588080.SH", "daily")]["Date"].min(), calendar_end
    )
    observed_sessions = pd.DatetimeIndex(
        pd.to_datetime(frames[("etf.ohlcv", "588080.SH", "daily")]["Date"])
    ).normalize()
    frames[("calendar.trading_sessions", "SSE", "daily")] = pd.DataFrame(
        {"Date": dates, "IsOpen": dates.normalize().isin(observed_sessions).astype(int)}
    )

    def fetch(request):
        key = (str(request.dataset), request.symbol, request.frequency)
        if key not in frames and str(request.dataset).startswith("etf."):
            key = (str(request.dataset), "588080.SH", request.frequency)
        frame = frames[key].copy()
        column = "Date"
        values = pd.to_datetime(frame[column])
        request_end = pd.Timestamp(request.end)
        if len(str(request.end)) == 10:
            request_end += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        frame = frame.loc[
            values.between(pd.Timestamp(request.start), request_end)
        ].reset_index(drop=True)
        metadata = {"vendor": "functional-fixture"}
        if str(request.dataset) == "etf.unadjusted_daily":
            metadata["adjustment"] = "none"
        return frame, metadata

    flows = Dataflows({key[0]: fetch for key in frames})
    monkeypatch.setattr("strategy_runtime.preparation.Dataflows", lambda: flows)
    for relative in (
        Path("S001/0824_EX04/artifacts/frozen_challenger.json"),
        Path("S001/0901_EX20/artifacts/frozen_challenger.json"),
        Path("S001/0902_EX02/artifacts/frozen_execution_policy.json"),
        Path("S001/0903_EX06/artifacts/frozen_challenger.json"),
        Path("S007/20260915_S007_EX04/artifacts/causal_feature_panel.csv.gz"),
    ):
        source = REPO_ROOT / "experiments" / relative
        if source.is_file():
            destination = root / "experiments" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "outputs").mkdir()
    return root
