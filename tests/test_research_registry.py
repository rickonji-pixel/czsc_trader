from __future__ import annotations

import json
from pathlib import Path

import pytest

from czsc_trader.application.context import RepositoryContext
import czsc_trader.research.handlers as handler_module
from czsc_trader.research.contracts import ResearchProtocolError
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

    with pytest.raises(ResearchProtocolError, match="duplicate"):
        registry.register(FakeHandler("same"))


def test_registry_rejects_unknown_protocol_type() -> None:
    registry = ExperimentRegistry()

    with pytest.raises(ResearchProtocolError, match="no registered handler"):
        registry.resolve({"experiment_type": "unknown"}, "0901_EX01")


def test_default_registry_resolves_every_tracked_experiment() -> None:
    registry = build_default_registry()

    for experiment_dir in sorted(Path("experiments").iterdir()):
        protocol = json.loads(
            (experiment_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
        )
        handler = registry.resolve(protocol, experiment_dir.name)
        assert handler.handler_id


def test_ex08_handler_enforces_formal_memory_execution_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment_dir = tmp_path / "experiments" / "0824_EX08"
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True)
    protocol = {
        "experiment_id": "0824_EX08",
        "experiment_type": "optuna_inmemory_full_joint_strategy_search",
        "storage_mode": "memory",
    }
    (artifacts / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(raw_dir, baseline_root, selected_dir, **kwargs):
        captured.update(kwargs)
        return {"status": "FAIL", "experiment_dir": str(selected_dir)}

    monkeypatch.setattr("czsc_trader.optuna_runner.run_optuna_experiment", fake_run)
    monkeypatch.setattr(handler_module, "_git_head", lambda root: "abc123")
    context = RepositoryContext(
        root=tmp_path,
        raw_dir=tmp_path / "data" / "raw",
        baseline_root=tmp_path / "configs" / "rule_baselines",
        experiments_root=tmp_path / "experiments",
        outputs_root=tmp_path / "outputs",
    )
    handler = build_default_registry().resolve(protocol, "0824_EX08")

    handler.run(context, experiment_dir)

    assert captured["storage_mode"] == "memory"
    assert captured["recover_runtime"] is False
    assert captured["collect_batch_timings"] is True
    assert captured["require_full_trial_count"] is True
    assert captured["execution_commit"] == "abc123"
