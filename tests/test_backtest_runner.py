from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

import czsc_trader.backtest_runner as runner
from czsc_trader.backtest_runner import BacktestRequest, run_fixed_backtest


def _request(tmp_path: Path, **changes: object) -> BacktestRequest:
    values: dict[str, object] = {
        "symbol": "588080.SH",
        "asset_type": "etf",
        "start": date(2026, 1, 1),
        "end": date(2026, 1, 30),
        "raw_dir": Path("data/raw"),
        "outputs_root": tmp_path,
        "baseline_root": Path("configs/rule_baselines"),
    }
    values.update(changes)
    return BacktestRequest(**values)


def test_report_includes_sharpe_for_every_backtest_period() -> None:
    report = runner._report(
        "600519.SH",
        "baseline_20260823",
        {
            "acceptance_status": "N/A",
            "windows": {
                "full": {
                    "start": "2026-01-05",
                    "end": "2026-08-21",
                    "strategy_return": 0.25,
                    "max_drawdown": -0.1,
                    "sharpe": 2.74656,
                    "trade_count": 8,
                    "status": "N/A",
                }
            },
        },
        ["chart.html"],
    )

    assert "| 夏普率 |" in report
    assert "| 2.747 |" in report


def test_fixed_backtest_uses_latest_baseline_and_writes_nonresearch_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lightweight_chart(*args, **kwargs) -> Path:
        output_path = Path(args[-1])
        output_path.write_text("<html>chart</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(runner, "write_period_chart", lightweight_chart)

    summary = run_fixed_backtest(_request(tmp_path), run_date=date(2026, 8, 23))

    output = tmp_path / "588080_0823_R01"
    assert Path(summary["output_dir"]) == output.resolve()
    assert summary["acceptance_status"] == "N/A"
    expected = {
        "manifest.json",
        "baseline_rule.json",
        "metrics.json",
        "orders.csv",
        "equity.csv",
        "factors.csv",
        "factor_events.csv",
        "audit.json",
        "chart.html",
        "report.md",
    }
    actual = {path.name for path in output.iterdir()}
    assert expected <= actual
    assert "candidate_results.csv" not in actual
    assert "selected_rule.json" not in actual
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["baseline"]["version"] == "baseline_20260823"
    assert manifest["baseline"]["sha256"]
    assert manifest["baseline"]["verification_snapshot"] == (
        "docs/baselines/588080_2026_expected.json"
    )
    assert manifest["data"]["hashes"]
    assert manifest["symbol"] == "588080.SH"
    audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
    assert audit["status"] == "PASS"


def test_fixed_backtest_increments_output_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "588080_0823_R01").mkdir()
    monkeypatch.setattr(
        runner,
        "write_period_chart",
        lambda *args: Path(args[-1]).write_text("<html></html>", encoding="utf-8") or Path(args[-1]),
    )

    summary = run_fixed_backtest(_request(tmp_path), run_date=date(2026, 8, 23))

    assert Path(summary["output_dir"]).name == "588080_0823_R02"


def test_target_config_is_mutually_exclusive_with_cli_dates(tmp_path: Path) -> None:
    target_path = tmp_path / "targets.json"
    target_path.write_text(
        json.dumps(
            {
                "periods": {
                    "sample": {
                        "start": "2026-01-01",
                        "end": "2026-01-30",
                        "min_return": 0.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot be combined"):
        run_fixed_backtest(_request(tmp_path, targets_path=target_path))


def test_explicit_unknown_baseline_fails_with_failure_record(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown rule baseline"):
        run_fixed_backtest(
            _request(tmp_path, baseline="baseline_20990101"),
            run_date=date(2026, 8, 23),
        )

    failure = tmp_path / "588080_0823_R01" / "failure.json"
    assert failure.is_file()
    assert "Unknown rule baseline" in failure.read_text(encoding="utf-8")
