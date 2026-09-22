from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from strategy_runtime import CHART_CONTEXT_VERSION, ChartRuntime
from strategy_runtime import load_strategy_deployment


ROOT = Path(__file__).resolve().parents[4]


def _configuration(reference: str) -> dict:
    strategy_id, version = reference.rsplit("-", 1)
    value = json.loads(
        (ROOT / "strategies" / strategy_id / "versions" / f"{version}.json").read_text(
            encoding="utf-8"
        )
    )
    return value["strategy_payload"]


def _rows(strategy_id: str) -> list[dict]:
    common = {
        "S001": {"factor_score": 0.2, "regime": "trend"},
        "S002": {"factor_score": 1.0},
        "S003": {"factor_score": 0.4, "threshold": 0.3},
        "S007": {"factor_score": 0.2, "confirmation_score": 0.1},
    }[strategy_id]
    return [
        {
            "decision_id": "DEC-1",
            "signal_date": "2026-09-03",
            "date": "2026-09-03",
            "action": "BUY",
            "target_position": 1,
            "target_quantity": 1000,
            **common,
        },
        {
            "decision_id": "DEC-2",
            "signal_date": "2026-09-04",
            "date": "2026-09-04",
            "action": "HOLD",
            "target_position": 1,
            "target_quantity": 1000,
            **common,
        },
    ]


def _context(reference: str) -> dict:
    strategy_id = reference.partition("-")[0]
    binding = load_strategy_deployment(ROOT / "strategies", reference).binding
    rows = _rows(strategy_id)
    context = {
        "contract_version": CHART_CONTEXT_VERSION,
        "mode": "BACKTEST",
        "strategy": {
            "strategy_id": strategy_id,
            "reference_id": reference,
            "identity_hash": binding["release_hash"],
            "symbol": "TEST.SH",
            "configuration": _configuration(reference),
        },
        "window": {"evaluation_start": "2026-09-02", "evaluation_end": "2026-09-04"},
        "market_data": {
            "identity": "market-fixture",
            "adjustment": "hfq",
            "bars": [
                {
                    "date": f"2026-09-0{day}",
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0 + day / 100,
                }
                for day in (2, 3, 4)
            ],
        },
        "strategy_output": {"decisions": rows, "chart_rows": rows, "support": {}},
        "execution": {
            "orders": [],
            "fills": [],
            "trades": [],
            "account_daily": [
                {"date": "2026-09-02", "equity": 100000},
                {"date": "2026-09-04", "equity": 101000},
            ],
            "intents": [],
            "snapshots": [
                {"session": "2026-09-03", "quantity": 1000},
                {"session": "2026-09-04", "quantity": 1000},
            ],
            "initial_cash": 100000,
        },
        "render": {"format": "html", "plotly_runtime": "external"},
    }
    return context


@pytest.mark.parametrize(
    "reference", ["S001-v1", "S001-v2", "S002-v1", "S003-v1", "S007-v1"]
)
def test_frozen_strategy_owns_backtest_chart(reference: str) -> None:
    runtime = ChartRuntime(ROOT / "strategies")

    backtest = runtime.render_backtest(reference, _context(reference))

    assert backtest.startswith("<!doctype html>")
    assert reference in backtest
    assert 'src="/static/plotly.min.js"' in backtest


def test_chart_runtime_rejects_release_hash_drift() -> None:
    context = _context("S007-v1")
    context["strategy"]["identity_hash"] = "0" * 64

    with pytest.raises(ValueError, match="identity hash differs"):
        ChartRuntime(ROOT / "strategies").render_backtest("S007-v1", context)


def test_chart_runtime_executes_common_code_from_the_authenticated_release() -> None:
    deployment = load_strategy_deployment(ROOT / "strategies", "S007-v1")
    implementation = ChartRuntime._implementation(
        deployment.binding["charts"],
        source_root=deployment.source_root,
        install_files=tuple(deployment.binding["install_files"]),
    )

    assert Path(inspect.getsourcefile(implementation.__class__.__mro__[1])).resolve() == (
        deployment.source_root / "charts/common.py"
    ).resolve()
