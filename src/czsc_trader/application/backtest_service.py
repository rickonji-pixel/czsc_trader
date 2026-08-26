from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import shutil
import tempfile

from czsc_trader.backtest_runner import BacktestRequest, run_fixed_backtest
from czsc_trader.reporting.publication import publish_run_directory

from .context import RepositoryContext
from .errors import ExecutionError
from .results import CommandResult


@dataclass(frozen=True)
class BacktestCommand:
    symbol: str
    asset_type: str
    start: date | None = None
    end: date | None = None
    baseline: str | None = None
    windows_path: Path | None = None
    window: str | None = None
    fee_rate: float = 0.0005
    init_cash: float = 1_000_000.0
    outputs_root: Path | None = None


def _repository_path(context: RepositoryContext, path: Path | None) -> Path | None:
    if path is None:
        return None
    value = Path(path)
    return value.resolve() if value.is_absolute() else (context.root / value).resolve()


def run_backtest(
    context: RepositoryContext,
    request: BacktestCommand,
    *,
    run_date: date | None = None,
) -> CommandResult:
    effective_date = run_date or datetime.now().astimezone().date()
    outputs_root = _repository_path(context, request.outputs_root) or context.outputs_root
    outputs_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".backtest_", dir=outputs_root))
    try:
        summary = run_fixed_backtest(
            BacktestRequest(
                symbol=request.symbol,
                asset_type=request.asset_type,
                start=request.start,
                end=request.end,
                baseline=request.baseline,
                windows_path=_repository_path(context, request.windows_path),
                window=request.window,
                fee_rate=request.fee_rate,
                init_cash=request.init_cash,
                raw_dir=context.raw_dir,
                outputs_root=staging_root,
                baseline_root=context.baseline_root,
            ),
            run_date=effective_date,
        )
        staged_output = Path(str(summary.pop("output_dir")))
        output_dir = publish_run_directory(
            staged_output,
            outputs_root,
            request.symbol,
            effective_date,
        )
    except Exception as exc:
        raise ExecutionError(
            "backtest_failed",
            str(exc),
            context={"symbol": request.symbol, "baseline": request.baseline},
        ) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return CommandResult(
        status="PASS",
        command="backtest.run",
        result=summary,
        artifacts={"output_dir": str(output_dir)},
    )
