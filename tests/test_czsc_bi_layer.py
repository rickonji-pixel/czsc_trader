from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.czsc_bi_layer import build_joint_factors, parse_signal_values
from czsc_trader.czsc_factor_stability import evaluate_conditional_factor_stability
from czsc_trader.research.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_parse_signal_values_keeps_first_two_czsc_values() -> None:
    assert parse_signal_values("向上_第3层_任意_0") == ("向上", "第3层")
    assert parse_signal_values("其他_任意_任意_0") == ("其他", "任意")
    assert parse_signal_values(None) == (None, None)


def test_joint_factors_build_exact_direction_by_layer_states() -> None:
    index = pd.bdate_range("2021-01-04", periods=4)
    raw = pd.Series(
        ["向上_第1层_任意_0", "向上_第2层_任意_0", "向下_第1层_任意_0", "其他_任意_任意_0"],
        index=index,
    )

    result = build_joint_factors(raw, ("向上", "向下"), ("第1层", "第2层"))

    assert result.factors.columns.tolist() == [
        "joint__cxt_bi_zdf::向上::第1层",
        "joint__cxt_bi_zdf::向上::第2层",
        "joint__cxt_bi_zdf::向下::第1层",
        "joint__cxt_bi_zdf::向下::第2层",
    ]
    assert result.factors.sum().to_dict() == {
        "joint__cxt_bi_zdf::向上::第1层": 1,
        "joint__cxt_bi_zdf::向上::第2层": 1,
        "joint__cxt_bi_zdf::向下::第1层": 1,
        "joint__cxt_bi_zdf::向下::第2层": 0,
    }
    assert result.directions["向上"].tolist() == [True, True, False, False]


def test_conditional_stability_uses_only_same_direction_controls() -> None:
    index = pd.to_datetime(["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"])
    indicator = pd.Series([1, 0, 0, 0], index=index, name="up_layer_1")
    same_direction = pd.Series([1, 1, 0, 0], index=index)
    outcomes = pd.DataFrame(
        {
            "return_20": [0.10, -0.10, 1.0, -1.0],
            "mae_20": [-0.01, -0.05, 0.0, -0.5],
            "max_drawdown_20": [-0.02, -0.08, 0.0, -0.6],
        },
        index=index,
    )

    metrics = evaluate_conditional_factor_stability(
        indicator,
        same_direction,
        outcomes,
        years=(2021,),
        horizons=(20,),
    )

    row = metrics.iloc[0]
    assert row["active_n"] == 1
    assert row["control_n"] == 1
    assert row["return_delta"] == pytest.approx(0.20)
    assert row["max_drawdown_delta"] == pytest.approx(0.06)


def test_bi_layer_stability_handler_is_registered() -> None:
    protocol = json.loads(
        (REPO_ROOT / "experiments" / "0901_EX02" / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )

    handler = build_default_registry().resolve(protocol, "0901_EX02")

    assert handler.handler_id == "czsc_bi_layer_stability_diagnostic"
