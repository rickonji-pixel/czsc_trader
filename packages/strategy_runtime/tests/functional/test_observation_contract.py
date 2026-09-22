from __future__ import annotations

import pytest

from strategy_runtime import (
    materialize_observation,
    unavailable_observation,
    validate_observation_descriptor,
    validate_observation_payload,
)


def _descriptor() -> dict[str, object]:
    return {
        "contract_version": "strategy_observation.v1",
        "series": [
            {
                "key": "base_score",
                "label": "基础分",
                "value_field": "base_score",
                "guides": [
                    {"key": "entry", "label": "入场阈值", "value": 0.2},
                    {
                        "key": "dynamic_exit",
                        "label": "动态退出线",
                        "value_field": "exit_threshold",
                    },
                ],
            }
        ],
    }


def test_observation_descriptor_materializes_strategy_neutral_facts() -> None:
    descriptor = validate_observation_descriptor(_descriptor())

    observation = materialize_observation(
        descriptor,
        {"base_score": 0.31, "exit_threshold": 0.08, "private_fact": "hidden"},
        action="BUY",
        target_position=1,
    )

    assert observation == {
        "contract_version": "strategy_observation.v1",
        "status": "READY",
        "action": "BUY",
        "target_position": 1.0,
        "series": [
            {
                "key": "base_score",
                "label": "基础分",
                "value": 0.31,
                "guides": [
                    {"key": "entry", "label": "入场阈值", "value": 0.2},
                    {"key": "dynamic_exit", "label": "动态退出线", "value": 0.08},
                ],
            }
        ],
    }
    assert validate_observation_payload(observation) == observation


def test_observation_materialization_rejects_missing_strategy_fact() -> None:
    with pytest.raises(ValueError, match="missing exit_threshold"):
        materialize_observation(
            _descriptor(), {"base_score": 0.31}, action="WAIT", target_position=0
        )


def test_unavailable_observation_is_a_valid_non_trading_failure_fact() -> None:
    observation = unavailable_observation("chart fact unavailable")

    assert validate_observation_payload(observation) == observation
