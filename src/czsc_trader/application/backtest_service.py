from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from czsc_trader.backtesting import (
    BacktestRequestV2,
    load_replay_data,
    resolve_registered_strategy,
    run_backtest_v2,
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
        overlay = snapshot.resolved_rule.constituent_moneyflow_intraday
        if snapshot.resolved_rule.execution is None and overlay is None:
            raise ValueError("strategy has no complete execution rule")
        data = load_replay_data(
            context,
            request.dataset,  # type: ignore[arg-type]
            request.symbol,
            request.asset_type,
            request.end,
            include_five_minute=overlay is not None,
        )
        summary = run_backtest_v2(
            snapshot=snapshot,
            replay_data=data,
            request=BacktestRequestV2(
                symbol=request.symbol,
                asset_type=request.asset_type,
                dataset=data.dataset,
                start=request.start,
                end=request.end,
                initial_cash=request.init_cash,
            ),
            outputs_root=_repository_path(context, request.outputs_root) or context.outputs_root,
            run_date=run_date or datetime.now().astimezone().date(),
            repository_root=context.root,
        )
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
            "dataset": data.dataset,
            "metrics": summary.metrics,
            "audit_status": summary.manifest["audit"]["status"],
        },
        artifacts={"output_dir": str(summary.output_dir)},
    )
