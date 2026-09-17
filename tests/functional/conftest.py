from __future__ import annotations

from pathlib import Path
import shutil

import pandas as pd
import pytest
from dataflows import DataIdentity, DataRequest, DataResult, DataStatus
from strategy_manager import StrategyRegistry
from strategy_runtime import (
    PublicationStatus,
    PublishedStrategyData,
    StrategyLoader,
    StrategyRelease,
    write_publication,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


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
    stored = StrategyRegistry(root / "strategies").get_version("S001", "v1")
    strategy = StrategyLoader().load(StrategyRelease.from_mapping(stored.to_dict()))
    requirements = {item.name: item for item in strategy.definition.inputs.requirements}
    requests = {}
    results = {}
    for name, frame in frames.items():
        requirement = requirements[name]
        request_end = calendar_end.date() if name == "trading_calendar" else cutoff
        required = None if name == "adjusted_weekly" else request_end.isoformat()
        request = DataRequest(
            requirement.dataset,
            requirement.subject,
            pd.Timestamp(frame["Date"].min()).date().isoformat(),
            request_end.isoformat(),
            required,
            requirement.frequency,
        )
        identity = DataIdentity(
            requirement.dataset,
            "fixture",
            requirement.subject,
            pd.Timestamp(frame["Date"].min()).isoformat(),
            pd.Timestamp(frame["Date"].max()).isoformat(),
            "a" * 64,
        )
        requests[name] = request
        results[name] = DataResult(DataStatus.READY, frame, identity)
    publication = PublishedStrategyData(
        strategy.definition.release_id,
        strategy.definition.release_hash,
        PublicationStatus.READY,
        cutoff.isoformat(),
        requests,
        results,
    )
    write_publication(publication, data_root)


@pytest.fixture
def functional_repo(tmp_path: Path) -> Path:
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
    _publish_s001_fixture(root)
    for relative in (
        Path("S001/0824_EX04/artifacts/frozen_challenger.json"),
        Path("S001/0901_EX20/artifacts/frozen_challenger.json"),
        Path("S001/0902_EX02/artifacts/frozen_execution_policy.json"),
        Path("S001/0903_EX06/artifacts/frozen_challenger.json"),
    ):
        source = REPO_ROOT / "experiments" / relative
        if source.is_file():
            destination = root / "experiments" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "outputs").mkdir()
    return root
