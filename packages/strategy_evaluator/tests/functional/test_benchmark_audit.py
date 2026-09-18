from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
from strategy_evaluator import AuditStatus, BenchmarkEvidence, audit_benchmark_replay


def _buyhold_evidence() -> BenchmarkEvidence:
    initial_cash = 100.0
    fee_rate = 0.01
    shares = initial_cash / (10.0 * (1.0 + fee_rate))
    equity = pd.Series([shares * 11.0, shares * 12.0])
    prior = equity.shift(1)
    prior.iloc[0] = initial_cash
    returns = equity.div(prior).sub(1.0)
    sharpe = float(np.sqrt(252.0) * returns.mean() / returns.std(ddof=1))
    return BenchmarkEvidence(
        "BUYHOLD",
        initial_cash,
        fee_rate,
        ("2026-01-05", "2026-01-06"),
        (
            {"date": "2026-01-05", "open": 10.0, "close": 11.0},
            {"date": "2026-01-06", "open": 12.0, "close": 12.0},
        ),
        (),
        (
            {"date": "2026-01-05", "target_position": 1.0, "equity": equity.iloc[0]},
            {"date": "2026-01-06", "target_position": 1.0, "equity": equity.iloc[1]},
        ),
        (
            {
                "signal_date": "2026-01-02",
                "execution_date": "2026-01-05",
                "side": "Buy",
                "size": shares,
                "price": 10.0,
                "fees": shares * 10.0 * fee_rate,
            },
        ),
        (),
        {
            "return": float(equity.iloc[-1] / initial_cash - 1.0),
            "max_drawdown": 0.0,
            "calmar": None,
            "sharpe": sharpe,
            "win_loss_ratio": None,
            "win_loss_ratio_status": "NO_CLOSED_TRADES",
            "closed_trades": 0,
        },
    )


def test_benchmark_audit_rejects_missing_account_session() -> None:
    evidence = _buyhold_evidence()
    assert audit_benchmark_replay(evidence).status is AuditStatus.PASS

    result = audit_benchmark_replay(
        replace(evidence, account_daily=evidence.account_daily[:1])
    )
    assert result.status is AuditStatus.FAIL
    assert "INCOMPLETE_BENCHMARK_ACCOUNT_COVERAGE" in result.reason_codes
