from __future__ import annotations

import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from czsc_trader.audit import audit_no_lookahead
from czsc_trader.backtest import run_backtest, run_period_backtests
from czsc_trader.rules import Rule


REPO_ROOT = Path(__file__).resolve().parents[1]


def _price_frame(
    open_values: list[float], close_values: list[float]
) -> pd.DataFrame:
    index = pd.date_range("2026-01-05", periods=len(open_values), freq="B", name="dt")
    return pd.DataFrame(
        {"open": open_values, "close": close_values},
        index=index,
    )


def _score_series(values: list[float]) -> pd.Series:
    index = pd.date_range("2025-01-02", periods=len(values), freq="B", name="dt")
    return pd.Series(values, index=index, name="factor_score", dtype=float)


def _state_rule(*, min_hold_days: int = 3) -> Rule:
    return Rule(
        weights=(0.3, 0.3, 0.4),
        enter=0.175,
        exit=0.025,
        confirm_days=1,
        min_hold_days=min_hold_days,
        exit_confirm_days=1,
        entry_gate="none",
    )


def test_fractional_target_buys_once_and_does_not_rebalance_daily() -> None:
    daily = _price_frame(
        open_values=[100.0, 100.0, 120.0, 120.0],
        close_values=[100.0, 100.0, 120.0, 120.0],
    )
    target = pd.Series([0.5, 0.5, 0.0, 0.0], index=daily.index)

    result = run_backtest(
        daily,
        target,
        fee_rate=0.0005,
        init_cash=100_000.0,
    )

    assert result.orders["side"].tolist() == ["Buy", "Sell"]
    assert result.orders["size"].tolist() == pytest.approx([500.0, 500.0])
    assert result.equity.tolist() == pytest.approx(
        [100_000.0, 99_975.0, 109_975.0, 109_945.0]
    )


def test_entry_size_is_frozen_until_exit() -> None:
    from czsc_trader.position_sizing import positions_from_entry_sizing

    scores = _score_series([0.0, 0.18, 0.30, 0.10, 0.02])

    target = positions_from_entry_sizing(
        scores,
        enter=0.175,
        full=0.225,
        exit_=0.025,
        state_rule=_state_rule(),
    )

    assert target.tolist() == [0.0, 0.5, 0.5, 0.5, 0.0]


def test_strong_entry_is_full_and_half_entry_event_is_not_an_exit() -> None:
    from czsc_trader.position_sizing import (
        build_entry_sizing_events,
        positions_from_entry_sizing,
    )

    scores = _score_series([0.23, 0.10, 0.02, 0.18])
    target = positions_from_entry_sizing(
        scores,
        enter=0.175,
        full=0.225,
        exit_=0.025,
        state_rule=_state_rule(min_hold_days=2),
    )

    events = build_entry_sizing_events(
        target,
        scores,
        enter=0.175,
        full=0.225,
        exit_=0.025,
    )

    assert target.tolist() == [1.0, 1.0, 0.0, 0.5]
    assert events["event_type"].tolist() == ["Entry", "Exit", "Entry"]
    assert events["after_position"].tolist() == [1.0, 0.0, 0.5]


def test_fractional_orders_require_exact_transition_provenance() -> None:
    from czsc_trader.position_sizing import build_entry_sizing_events

    daily = _price_frame(
        open_values=[100.0, 100.0, 100.0, 110.0, 110.0, 110.0],
        close_values=[100.0, 100.0, 100.0, 110.0, 110.0, 110.0],
    )
    scores = pd.Series(
        [0.0, 0.18, 0.10, 0.10, 0.02, 0.0],
        index=daily.index,
        name="factor_score",
    )
    target = pd.Series(
        [0.0, 0.5, 0.5, 0.5, 0.0, 0.0],
        index=daily.index,
        name="target_position",
    )
    events = build_entry_sizing_events(target, scores, 0.175, 0.225, 0.025)
    factor_frame = pd.DataFrame(
        {
            "factor_score": scores,
            "enter_threshold": 0.175,
            "full_threshold": 0.225,
            "exit_threshold": 0.025,
        },
        index=daily.index,
    )
    result = run_period_backtests(
        daily,
        target,
        {"P": (daily.index[1], daily.index[-1])},
        factor_events=events,
        factor_frame=factor_frame,
    )["P"]

    audit = audit_no_lookahead(
        result.orders,
        result.factor_events,
        target,
        factor_frame,
    )

    assert audit["status"] == "PASS"
    mutated = result.factor_events.copy()
    mutated.loc[mutated["event_type"] == "Entry", "after_position"] = 1.0
    with pytest.raises(AssertionError, match="event positions differ"):
        audit_no_lookahead(result.orders, mutated, target, factor_frame)


def test_position_sizing_protocol_rejects_any_preregistered_drift() -> None:
    from czsc_trader.position_sizing_runner import (
        validate_position_sizing_protocol,
    )

    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0829_EX01"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )

    validate_position_sizing_protocol(protocol)
    changed = {**protocol, "full_thresholds": [0.2, 0.225, 0.275]}
    with pytest.raises(ValueError, match="full thresholds"):
        validate_position_sizing_protocol(changed)


def test_position_sizing_candidates_use_original_return_only_ranking() -> None:
    from czsc_trader.position_sizing_runner import rank_position_sizing_results

    rows = pd.DataFrame(
        [
            {
                "candidate_id": "full_0.200",
                "win_count": 2,
                "min_return_delta": -0.10,
                "median_return_delta": 0.02,
                "mean_return_delta": 0.03,
                "mean_sharpe_delta": 9.0,
            },
            {
                "candidate_id": "full_0.225",
                "win_count": 2,
                "min_return_delta": -0.05,
                "median_return_delta": 0.01,
                "mean_return_delta": 0.02,
                "mean_sharpe_delta": -9.0,
            },
            {
                "candidate_id": "full_0.250",
                "win_count": 3,
                "min_return_delta": -0.20,
                "median_return_delta": -0.01,
                "mean_return_delta": 0.00,
                "mean_sharpe_delta": -20.0,
            },
        ]
    )

    ranked = rank_position_sizing_results(rows)

    assert ranked["candidate_id"].tolist() == [
        "full_0.250",
        "full_0.225",
        "full_0.200",
    ]


def test_position_sizing_pass_requires_strict_return_win_in_all_original_windows() -> None:
    from czsc_trader.position_sizing_runner import position_sizing_holdout_pass

    windows = {
        name: {"champion_return": 0.10, "challenger_return": 0.11}
        for name in ("2026Q1", "2026H1", "2026M1-M8")
    }

    assert position_sizing_holdout_pass(windows)
    windows["2026H1"]["challenger_return"] = 0.10
    assert not position_sizing_holdout_pass(windows)


def test_position_sizing_protocol_resolves_to_its_dedicated_handler() -> None:
    from czsc_trader.research.registry import build_default_registry

    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0829_EX01"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )

    handler = build_default_registry().resolve(protocol, "0829_EX01")

    assert handler.handler_id == "entry_fixed_position_sizing"


def test_error_archive_truthfully_records_if_holdout_was_accessed(
    tmp_path: Path,
) -> None:
    from czsc_trader.position_sizing_runner import _finalize_error

    source = REPO_ROOT / "experiments" / "0829_EX01"
    experiment_dir = tmp_path / "0829_EX01"
    (experiment_dir / "artifacts").mkdir(parents=True)
    for name in ("01_goal.md", "02_design.md", "implementation_plan.md"):
        shutil.copy2(source / name, experiment_dir / name)
    shutil.copy2(
        source / "artifacts" / "protocol.json",
        experiment_dir / "artifacts" / "protocol.json",
    )
    protocol = json.loads(
        (experiment_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )

    _finalize_error(
        experiment_dir,
        protocol,
        RuntimeError("ledger mismatch"),
        "deadbeef",
        holdout_accessed=True,
    )

    manifest = json.loads(
        (experiment_dir / "experiment_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "ERROR"
    assert manifest["holdout_accessed"] is True
