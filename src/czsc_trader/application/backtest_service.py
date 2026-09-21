from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from czsc_trader.backtesting import (
    BacktestRequestV2,
    resolve_registered_strategy,
    run_backtest_v2,
)
from czsc_trader.backtesting.execution_data import (
    BacktestExecutionDataNotReadyError,
)

from .context import RepositoryContext
from .errors import ExecutionError
from .results import CommandResult


@dataclass(frozen=True)
class BacktestCommand:
    strategy_id: str
    strategy_version: str
    dataset: str
    symbol: str
    asset_type: str
    start: date
    end: date
    init_cash: float
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
    try:
        snapshot = resolve_registered_strategy(
            context, request.strategy_id, request.strategy_version
        )
        summary = run_backtest_v2(
            snapshot=snapshot,
            request=BacktestRequestV2(
                symbol=request.symbol,
                asset_type=request.asset_type,
                dataset=request.dataset,  # type: ignore[arg-type]
                start=request.start,
                end=request.end,
                initial_cash=request.init_cash,
            ),
            data_dir=context.backtest_data_root,
            outputs_root=_repository_path(context, request.outputs_root) or context.outputs_root,
            run_date=run_date or datetime.now().astimezone().date(),
            repository_root=context.root,
        )
    except BacktestExecutionDataNotReadyError as exc:
        raise ExecutionError(
            "backtest_data_not_ready",
            str(exc),
            context={
                "strategy": f"{request.strategy_id}-{request.strategy_version}",
                "symbol": request.symbol,
                "dataset": request.dataset,
                "requested_cutoff": exc.requested_cutoff.isoformat(),
                "published_cutoff": exc.published_cutoff.isoformat(),
                "first_unpublished_session": exc.first_unpublished_session.isoformat(),
            },
        ) from exc
    except Exception as exc:
        raise ExecutionError(
            "backtest_failed",
            str(exc),
            context={
                "strategy": f"{request.strategy_id}-{request.strategy_version}",
                "symbol": request.symbol,
                "dataset": request.dataset,
            },
        ) from exc
    return CommandResult(
        status="PASS",
        command="backtest.run",
        result={
            "strategy": snapshot.identity.reference,
            "dataset": request.dataset,
            "metrics": summary.metrics,
            "audit_status": summary.manifest["audit"]["status"],
            "runtime_engine": summary.manifest["application"]["runtime_engine"],
        },
        artifacts={"output_dir": str(summary.output_dir)},
    )
