from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = Path(sys.executable).with_name("czsc-trader.exe" if sys.platform == "win32" else "czsc-trader")
STRATEGY_METRIC_KEYS = {"max_drawdown", "calmar", "win_loss_ratio", "return", "sharpe"}
STRATEGY_METRIC_KEYS.add("win_loss_ratio_status")


def test_installed_cli_publishes_advice_v3_complete_baseline_contract() -> None:
    completed = subprocess.run(
        [
            str(CLI),
            "advice",
            "run",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--actual-quantity",
            "0",
            "--available-cash",
            "1000000",
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
    assert payload["status"] == "PASS"
    assert payload["result"]["contract_version"] == "advice.v3"
    assert payload["result"]["baseline"]["version"] == "baseline_20260903"
    assert "execution_policy" not in payload["result"]
    assert payload["result"]["actual_quantity"] == 0
    assert payload["result"]["target_quantity"] == 0
    assert payload["result"]["delta_quantity"] == 0
    assert payload["result"]["order"] is None


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
    assert payload["result"]["baseline"] == "baseline_20260903"
    assert set(full) == {"start", "end", "strategies"}
    strategies = full["strategies"]
    assert set(strategies) == {
        "active_baseline",
        "active_baseline_execution",
        "buyhold",
        "ma5_ma20",
    }
    assert all(set(metrics) == STRATEGY_METRIC_KEYS for metrics in strategies.values())
    assert strategies["active_baseline"]["return"] == pytest.approx(
        0.7528525916956634
    )
    expected_ma = {
        "max_drawdown": -0.22945460734778733,
        "calmar": 1.520558369439513,
        "win_loss_ratio": 3.2767930702460384,
        "return": 0.20069278199087237,
        "sharpe": 1.0253626758401702,
    }
    assert {key: strategies["ma5_ma20"][key] for key in expected_ma} == pytest.approx(
        expected_ma
    )
    assert strategies["ma5_ma20"]["win_loss_ratio_status"] == "VALID"
    assert strategies["buyhold"]["win_loss_ratio"] is None
    assert strategies["buyhold"]["win_loss_ratio_status"] == "NO_CLOSED_TRADES"

    assert (output_dir / "audit.json").is_file()
    assert (output_dir / "report.md").is_file()
    assert (output_dir / "chart.html").is_file()
    assert (output_dir / "ma_signals.csv").is_file()
    assert (output_dir / "ma_orders.csv").is_file()
    assert (output_dir / "ma_equity.csv").is_file()
    assert (output_dir / "ma_chart.html").is_file()
    assert (output_dir / "execution_orders.csv").is_file()
    assert (output_dir / "execution_equity.csv").is_file()
    execution_orders = pd.read_csv(output_dir / "execution_orders.csv")
    assert (execution_orders["size"] % 100 == 0).all()
    buy_limits = execution_orders.loc[
        execution_orders["side"] == "Buy", "entry_limit"
    ]
    assert (buy_limits * 1000 % 1 < 1e-9).all()
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["metrics_schema_version"] == 5
    execution = manifest["comparison_strategies"]["active_baseline_execution"]
    assert execution == {
        "baseline_version": "baseline_20260903",
        "baseline_sha256": manifest["baseline"]["sha256"],
        "execution": "embedded_complete_baseline",
    }

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
    assert report.count("| 活动基线·次日开盘 |") == 1
    assert "活动基线·执行规则" not in report
    assert report.count("| BuyHold |") == 1
    assert report.count("| MA5/MA20 |") == 1
    assert "| BuyHold |" in report and "无闭合交易" in report
    assert "| 策略 | 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 |" in report

    ma_chart = (output_dir / "ma_chart.html").read_text(encoding="utf-8")
    assert all(
        label in ma_chart
        for label in ("MA5", "MA20", "MA\\u4e70\\u5165", "MA\\u5356\\u51fa")
    )
    assert '"hovermode":"x unified"' in ma_chart
