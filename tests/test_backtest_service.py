from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from czsc_trader.application.backtest_service import BacktestCommand, run_backtest
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.errors import ExecutionError


def _context(outputs_root: Path) -> RepositoryContext:
    current = RepositoryContext.discover(Path.cwd())
    return RepositoryContext(
        root=current.root,
        raw_dir=current.raw_dir,
        baseline_root=current.baseline_root,
        experiments_root=current.experiments_root,
        outputs_root=outputs_root,
    )


def test_backtest_service_preserves_active_baseline_result(tmp_path: Path) -> None:
    result = run_backtest(
        _context(tmp_path),
        BacktestCommand(
            symbol="588080.SH",
            asset_type="etf",
            start=date(2026, 1, 1),
            end=date(2026, 8, 21),
        ),
        run_date=date(2026, 8, 26),
    )

    metrics = result.result["windows"]["full"]
    output_dir = Path(result.artifacts["output_dir"])
    assert metrics["strategy_return"] == pytest.approx(0.6197253904580182)
    assert metrics["trade_count"] == 10
    assert output_dir.name == "588080_0826_R01"
    assert (output_dir / "audit.json").is_file()
    assert not list(tmp_path.glob(".backtest_*"))


def test_backtest_service_does_not_publish_failed_run(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="Unknown rule baseline"):
        run_backtest(
            _context(tmp_path),
            BacktestCommand(
                symbol="588080.SH",
                asset_type="etf",
                start=date(2026, 1, 1),
                end=date(2026, 1, 30),
                baseline="baseline_20990101",
            ),
            run_date=date(2026, 8, 26),
        )

    assert list(tmp_path.iterdir()) == []
