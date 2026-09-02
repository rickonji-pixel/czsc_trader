from __future__ import annotations

from czsc_trader.reporting.backtest_report import render_backtest_report


def test_render_backtest_report_formats_all_comparison_rows() -> None:
    strategy_metrics = {
        "max_drawdown": -0.1,
        "calmar": 2.0,
        "win_loss_ratio": 3.0,
        "return": 0.2,
        "sharpe": 1.5,
    }
    strategies = {
        "active_baseline": strategy_metrics,
        "active_baseline_execution_policy": strategy_metrics,
        "buyhold": {**strategy_metrics, "win_loss_ratio": None},
        "ma5_ma20": {**strategy_metrics, "win_loss_ratio": None},
    }

    text = render_backtest_report(
        "588080.SH",
        "baseline_test",
        {
            "windows": {
                "full": {
                    "start": "2026-01-01",
                    "end": "2026-01-02",
                    "strategies": strategies,
                }
            }
        },
        ["chart.html", "ma_chart.html"],
    )

    assert "| 活动基线·次日开盘 |" in text
    assert "| 活动基线·执行规则 |" in text
    assert "| BuyHold |" in text
    assert "| MA5/MA20 |" in text
    assert "| -10.00% | 2.000 | 3.000 | 20.00% | 1.500 |" in text
    assert "[chart.html](chart.html)" in text
