from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.audit import audit_no_lookahead
from czsc_trader.backtest import run_period_backtests
from czsc_trader.rules import Rule


REPO_ROOT = Path(__file__).resolve().parents[1]


def _rule() -> Rule:
    return Rule(
        weights=(0.3, 0.3, 0.4),
        enter=0.175,
        exit=0.025,
        confirm_days=1,
        min_hold_days=3,
        exit_confirm_days=1,
        entry_gate="none",
    )


def test_dynamic_state_waits_for_min_hold_then_reduces_recovers_and_exits() -> None:
    from czsc_trader.position_sizing import positions_from_dynamic_score_zones

    index = pd.date_range("2025-01-02", periods=7, freq="B", name="dt")
    scores = pd.Series(
        [0.18, 0.10, 0.10, 0.10, 0.20, 0.10, 0.02],
        index=index,
        name="factor_score",
    )

    target = positions_from_dynamic_score_zones(
        scores,
        enter=0.175,
        exit_=0.025,
        state_rule=_rule(),
        partial_position=0.5,
    )

    assert target.tolist() == [1.0, 1.0, 1.0, 0.5, 1.0, 0.5, 0.0]


def test_dynamic_events_name_every_exact_transition() -> None:
    from czsc_trader.position_sizing import build_dynamic_sizing_events

    index = pd.date_range("2025-01-02", periods=7, freq="B", name="dt")
    scores = pd.Series(
        [0.18, 0.10, 0.10, 0.10, 0.20, 0.10, 0.02],
        index=index,
        name="factor_score",
    )
    target = pd.Series(
        [1.0, 1.0, 1.0, 0.5, 1.0, 0.5, 0.0],
        index=index,
        name="target_position",
    )

    events = build_dynamic_sizing_events(target, scores, 0.175, 0.025)

    assert events["event_type"].tolist() == [
        "Entry",
        "Reduce",
        "Increase",
        "Reduce",
        "Exit",
    ]
    assert events["before_position"].tolist() == [0.0, 1.0, 0.5, 1.0, 0.5]
    assert events["after_position"].tolist() == [1.0, 0.5, 1.0, 0.5, 0.0]


def test_resize_orders_have_exact_causal_provenance() -> None:
    from czsc_trader.position_sizing import build_dynamic_sizing_events

    index = pd.date_range("2026-01-05", periods=10, freq="B", name="dt")
    daily = pd.DataFrame({"open": 100.0, "close": 100.0}, index=index)
    scores = pd.Series(
        [0.0, 0.18, 0.10, 0.10, 0.10, 0.10, 0.20, 0.20, 0.02, 0.0],
        index=index,
        name="factor_score",
    )
    target = pd.Series(
        [0.0, 1.0, 1.0, 1.0, 0.5, 0.5, 1.0, 1.0, 0.0, 0.0],
        index=index,
        name="target_position",
    )
    events = build_dynamic_sizing_events(target, scores, 0.175, 0.025)
    factor_frame = pd.DataFrame(
        {
            "factor_score": scores,
            "enter_threshold": 0.175,
            "exit_threshold": 0.025,
        },
        index=index,
    )

    result = run_period_backtests(
        daily,
        target,
        {"P": (index[1], index[-1])},
        factor_events=events,
        factor_frame=factor_frame,
    )["P"]

    assert result.orders["side"].tolist() == ["Buy", "Sell", "Buy", "Sell"]
    assert result.orders["event_type"].tolist() == [
        "Entry",
        "Reduce",
        "Increase",
        "Exit",
    ]
    assert audit_no_lookahead(
        result.orders, result.factor_events, target, factor_frame
    )["status"] == "PASS"

    mutated = result.factor_events.copy()
    mutated.loc[mutated["event_type"] == "Reduce", "after_position"] = 0.25
    with pytest.raises(AssertionError, match="event positions differ"):
        audit_no_lookahead(result.orders, mutated, target, factor_frame)


def test_dynamic_protocol_rejects_any_preregistered_drift() -> None:
    from czsc_trader.dynamic_position_sizing_runner import (
        validate_dynamic_position_sizing_protocol,
    )

    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0829_EX02"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )
    validate_dynamic_position_sizing_protocol(protocol)

    changed = {**protocol, "partial_position": 0.6}
    with pytest.raises(ValueError, match="partial_position"):
        validate_dynamic_position_sizing_protocol(changed)


def test_dynamic_pass_keeps_original_strict_return_rule() -> None:
    from czsc_trader.dynamic_position_sizing_runner import (
        dynamic_position_sizing_holdout_pass,
    )

    windows = {
        name: {"champion_return": 0.10, "challenger_return": 0.11}
        for name in ("2026Q1", "2026H1", "2026M1-M8")
    }
    assert dynamic_position_sizing_holdout_pass(windows)
    windows["2026H1"]["challenger_return"] = 0.10
    assert not dynamic_position_sizing_holdout_pass(windows)


def test_dynamic_protocol_resolves_to_dedicated_handler() -> None:
    from czsc_trader.research.registry import build_default_registry

    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0829_EX02"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )

    handler = build_default_registry().resolve(protocol, "0829_EX02")

    assert handler.handler_id == "dynamic_score_zone_position_sizing"
