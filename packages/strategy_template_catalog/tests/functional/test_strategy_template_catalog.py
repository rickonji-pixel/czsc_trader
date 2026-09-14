from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy_template_catalog import (
    ParameterDefinition,
    TemplateRegistry,
    TemplateValidationError,
)


REPO = Path(__file__).resolve().parents[4]


def _binding(
    slot: str,
    source_id: str,
    *,
    kind: str = "SIGNAL",
    weight: object = None,
) -> dict[str, object]:
    return {
        "slot": slot,
        "source_id": source_id,
        "source_kind": kind,
        "state": None,
        "weight": weight,
    }


def test_stc01_repository_catalog_covers_five_opc_template_families() -> None:
    registry = TemplateRegistry(REPO / "strategy_templates")

    assert len(registry.templates) == 5
    assert len(registry.digest) == 64
    assert {item["operator"] for item in registry.list_templates(status="READY")} == {
        "WEIGHTED_SCORE",
        "GATED_SCORE",
        "REGIME_WEIGHTED_SCORE",
        "EVENT_HOLD",
        "CORE_OVERLAY",
    }
    event = registry.show("STC-T04-EVENT-HOLD")
    assert event["output"] == "TARGET_POSITION"
    assert any(item["name"] == "holding_sessions" for item in event["parameters"])


def test_stc02_instance_is_strict_defaulted_and_deterministic() -> None:
    registry = TemplateRegistry(REPO / "strategy_templates")
    first = registry.instantiate(
        "STC-T01-WEIGHTED-SCORE",
        [
            _binding("scores", "SIG-B", weight=0.4),
            _binding("scores", "F-A", kind="FACTOR", weight=0.6),
        ],
        {"entry_threshold": 0.7},
    )
    second = registry.instantiate(
        "STC-T01-WEIGHTED-SCORE",
        [
            _binding("scores", "F-A", kind="FACTOR", weight=0.6),
            _binding("scores", "SIG-B", weight=0.4),
        ],
        {"entry_threshold": 0.7, "target_position": 1.0, "exit_threshold": 0.0},
    )

    assert first.instance_id == second.instance_id
    assert first.digest == second.digest
    assert dict(first.parameters) == {
        "entry_threshold": 0.7,
        "exit_threshold": 0.0,
        "target_position": 1.0,
    }
    assert first.to_dict()["bindings"][0]["source_id"] == "F-A"


def test_stc03_invalid_structures_and_parameters_fail_clearly(tmp_path: Path) -> None:
    registry = TemplateRegistry(REPO / "strategy_templates")
    with pytest.raises(TemplateValidationError, match="requires 2..20"):
        registry.instantiate(
            "STC-T01-WEIGHTED-SCORE",
            [_binding("scores", "SIG-A", weight=1.0)],
        )
    with pytest.raises(TemplateValidationError, match="duplicate input sources"):
        registry.instantiate(
            "STC-T02-GATED-SCORE",
            [
                _binding("opportunity", "SIG-A", weight=1.0),
                _binding("confirmation", "SIG-A", weight=1.0),
            ],
        )
    with pytest.raises(TemplateValidationError, match="same set of at least two regimes"):
        registry.instantiate(
            "STC-T03-REGIME-WEIGHTED-SCORE",
            [
                _binding("regime", "SIG-REGIME"),
                _binding("scores", "SIG-A", weight={"trend": 0.7, "range": 0.3}),
                _binding("scores", "SIG-B", weight={"trend": 0.5, "stress": 0.5}),
            ],
        )
    with pytest.raises(TemplateValidationError, match="must not exceed 1"):
        registry.instantiate(
            "STC-T05-CORE-OVERLAY",
            [_binding("overlay_entries", "SIG-A")],
            {"core_position": 0.7, "overlay_position": 0.5},
        )
    with pytest.raises(TemplateValidationError, match="unknown template parameters"):
        registry.instantiate(
            "STC-T04-EVENT-HOLD",
            [_binding("entry_events", "SIG-A")],
            {"magic": 1},
        )

    broken = json.loads((REPO / "strategy_templates" / "templates.json").read_text("utf-8"))
    broken["items"][0]["parameters"][0]["unexpected"] = True
    (tmp_path / "templates.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(TemplateValidationError, match="unknown fields"):
        TemplateRegistry(tmp_path)

    with pytest.raises(TemplateValidationError, match="must be an integer"):
        ParameterDefinition.from_dict(
            {
                "name": "holding",
                "type": "INTEGER",
                "description": "holding",
                "default": 1.5,
                "minimum": 1,
                "maximum": 5,
                "choices": [],
            }
        )
