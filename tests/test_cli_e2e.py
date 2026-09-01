from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = Path(sys.executable).with_name(
    "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
)
STRATEGY_METRIC_KEYS = {
    "max_drawdown",
    "calmar",
    "win_loss_ratio",
    "return",
    "sharpe",
}
TRACKED_SYMBOLS = (
    "159352.SZ",
    "159516.SZ",
    "515050.SH",
    "588080.SH",
)


def _run_cli_json(*arguments: str) -> dict[str, object]:
    completed = subprocess.run(
        [str(CLI), *arguments, "--repo-root", str(REPO_ROOT), "--format", "json"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def test_cli_exposes_only_supported_resources() -> None:
    completed = subprocess.run(
        [str(CLI), "--help"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    resource_line = next(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith("{") and line.strip().endswith("}")
    )
    assert resource_line == "{data,baseline,backtest,archive}"
    assert "experiment" not in completed.stdout


def test_dataflows_package_imports_outside_the_checkout(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import dataflows; "
                "print(Path(dataflows.__file__).resolve())"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    module_path = Path(completed.stdout.strip())
    assert module_path.is_relative_to(REPO_ROOT / "packages" / "dataflows")


@pytest.mark.parametrize("symbol", TRACKED_SYMBOLS)
def test_tracked_market_data_validates_through_latest_session(symbol: str) -> None:
    payload = _run_cli_json("data", "validate", "--symbol", symbol)

    assert payload["status"] == "PASS"
    result = payload["result"]
    assert result["symbol"] == symbol
    assert result["requested_end"] == "2026-09-01"
    assert result["validation_status"] == "PASS"
    assert result["frequencies"] == ["30m", "daily", "weekly"]


def test_active_baseline_is_candidate143() -> None:
    payload = _run_cli_json(
        "baseline",
        "show",
        "--version",
        "baseline_20260901",
        "--symbol",
        "588080.SH",
    )

    assert payload["status"] == "PASS"
    result = payload["result"]
    assert result["version"] == "baseline_20260901"
    assert result["strategy"] == "czsc_regime_weight"
    assert result["status"] == "active"
    assert result["rule"]["candidate_id"] == 143


def test_all_frozen_experiment_archives_validate() -> None:
    payload = _run_cli_json("archive", "validate", "--all")

    assert payload["status"] == "PASS"
    assert payload["result"]["validated_count"] == 46
    assert payload["result"]["experiments"][-3:] == [
        "0901_EX19",
        "0901_EX20",
        "0901_EX21",
    ]


def test_installed_cli_runs_audited_backtest(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            str(CLI),
            "backtest",
            "run",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--start",
            "2026-01-01",
            "--end",
            "2026-08-21",
            "--outputs-root",
            str(tmp_path),
            "--repo-root",
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    output_dir = Path(payload["artifacts"]["output_dir"])
    full = payload["result"]["windows"]["full"]
    assert output_dir.name.startswith("588080_")
    assert output_dir.name.endswith("_BT01")
    assert payload["result"]["baseline"] == "baseline_20260901"
    assert set(full) == {"start", "end", "strategies"}
    strategies = full["strategies"]
    assert set(strategies) == {"active_baseline", "buyhold", "ma5_ma20"}
    assert all(set(metrics) == STRATEGY_METRIC_KEYS for metrics in strategies.values())
    assert strategies["active_baseline"]["return"] == pytest.approx(
        0.7528525916956634
    )
    assert strategies["ma5_ma20"] == pytest.approx(
        {
            "max_drawdown": -0.22945460734778733,
            "calmar": 1.520558369439513,
            "win_loss_ratio": 3.2767930702460384,
            "return": 0.20069278199087237,
            "sharpe": 1.234024171393839,
        }
    )
    assert strategies["buyhold"]["win_loss_ratio"] is None

    assert (output_dir / "audit.json").is_file()
    assert (output_dir / "report.md").is_file()
    assert (output_dir / "chart.html").is_file()
    assert (output_dir / "ma_signals.csv").is_file()
    assert (output_dir / "ma_orders.csv").is_file()
    assert (output_dir / "ma_equity.csv").is_file()
    assert (output_dir / "ma_chart.html").is_file()

    ma_signals = pd.read_csv(output_dir / "ma_signals.csv", parse_dates=["dt"])
    assert list(ma_signals.columns) == ["dt", "close", "ma5", "ma20", "target_position"]
    first_session = ma_signals.loc[ma_signals["dt"] == "2026-01-05"].iloc[0]
    assert first_session["target_position"] == 1.0
    assert first_session["ma5"] == pytest.approx(1.40065584)
    assert first_session["ma20"] == pytest.approx(1.37753371)

    ma_orders = pd.read_csv(
        output_dir / "ma_orders.csv",
        parse_dates=["signal_date", "execution_date"],
    )
    assert ma_orders.iloc[0]["side"] == "Buy"
    assert ma_orders.iloc[0]["signal_date"] == pd.Timestamp("2025-12-31")
    assert ma_orders.iloc[0]["execution_date"] == pd.Timestamp("2026-01-05")
    sessions = ma_signals["dt"].tolist()
    next_session = dict(zip(sessions, sessions[1:]))
    for order in ma_orders.itertuples():
        assert next_session[order.signal_date] == order.execution_date

    report = (output_dir / "report.md").read_text(encoding="utf-8")
    assert "## full" in report
    assert report.count("| 活动基线 |") == 1
    assert report.count("| BuyHold |") == 1
    assert report.count("| MA5/MA20 |") == 1
    assert "| 策略 | 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 |" in report

    ma_chart = (output_dir / "ma_chart.html").read_text(encoding="utf-8")
    assert all(
        label in ma_chart
        for label in ("MA5", "MA20", "MA\\u4e70\\u5165", "MA\\u5356\\u51fa")
    )
    assert '"hovermode":"x unified"' in ma_chart
