from __future__ import annotations

import json
from pathlib import Path

import pytest

from czsc_trader.application.errors import UsageError
from czsc_trader.research.registry import ExperimentRegistry, build_default_registry


class FakeHandler:
    def __init__(self, handler_id: str) -> None:
        self.handler_id = handler_id

    def validate_protocol(self, protocol):
        return None

    def run(self, context, experiment_dir):
        return {"status": "COMPLETE"}

    def replay(self, context, source_dir, output_dir):
        return {"status": "COMPLETE"}


def test_registry_resolves_new_protocol_and_historical_identity() -> None:
    registry = ExperimentRegistry(historical={"0824_EX01": "challenge"})
    challenge = FakeHandler("challenge")
    diagnostic = FakeHandler("diagnostic")
    registry.register(challenge)
    registry.register(diagnostic)

    assert registry.resolve({"handler": "diagnostic"}, "new") is diagnostic
    assert registry.resolve({}, "0824_EX01") is challenge


def test_registry_rejects_duplicate_handler_identity() -> None:
    registry = ExperimentRegistry()
    registry.register(FakeHandler("same"))

    with pytest.raises(UsageError, match="duplicate"):
        registry.register(FakeHandler("same"))


def test_registry_rejects_unknown_protocol_type() -> None:
    registry = ExperimentRegistry()

    with pytest.raises(UsageError, match="no registered handler"):
        registry.resolve({"experiment_type": "unknown"}, "0901_EX01")


def test_default_registry_resolves_every_tracked_experiment() -> None:
    registry = build_default_registry()

    for experiment_dir in sorted(Path("experiments").iterdir()):
        protocol = json.loads(
            (experiment_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
        )
        handler = registry.resolve(protocol, experiment_dir.name)
        assert handler.handler_id
