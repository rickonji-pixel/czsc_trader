from __future__ import annotations

from czsc_trader.reporting.backtest_report import render_backtest_report


def test_render_backtest_report_formats_all_comparison_rows() -> None:
    strategy_metrics = {
        "max_drawdown": -0.1,
        "calmar": 2.0,
        "win_loss_ratio": 3.0,
        "win_loss_ratio_status": "VALID",
        "return": 0.2,
        "sharpe": 1.5,
    }
    strategies = {
        "active_baseline": {
            **strategy_metrics,
            "win_loss_ratio": None,
            "win_loss_ratio_status": "NO_LOSSES",
        },
        "active_baseline_execution": strategy_metrics,
        "buyhold": {
            **strategy_metrics,
            "win_loss_ratio": None,
            "win_loss_ratio_status": "NO_CLOSED_TRADES",
        },
        "ma5_ma20": {
            **strategy_metrics,
            "win_loss_ratio": None,
            "win_loss_ratio_status": "NO_WINS",
        },
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
    assert "| 完整基线·实际执行 |" in text
    assert "| BuyHold |" in text
    assert "| MA5/MA20 |" in text
    assert "| 活动基线·次日开盘 | -10.00% | 2.000 | 无亏损 |" in text
    assert "| 完整基线·实际执行 | -10.00% | 2.000 | 3.000 |" in text
    assert "| BuyHold | -10.00% | 2.000 | 无闭合交易 |" in text
    assert "| MA5/MA20 | -10.00% | 2.000 | 无盈利 |" in text
    assert "[chart.html](chart.html)" in text
