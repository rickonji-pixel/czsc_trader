import json
from pathlib import Path

from strategy_manager import Qualification, StrategyRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_current_strategy_resolves_to_formal_identity_and_frozen_payload():
    registry = StrategyRegistry(REPO_ROOT / "configs" / "strategies")

    current = registry.get_version("S001", "v2")
    historical = registry.get_version("S001", "v1")
    by_alias = registry.resolve_strategy("baseline_20260903")
    strategy = registry.get_strategy("S001")

    assert strategy.name == "综合基线策略"
    assert by_alias == current
    assert current.release_id == "S001-v2"
    assert current.strategy_payload["rule"]["candidate_id"] == "R1102"
    assert len(current.release_hash) == 64
    assert historical.release_id == "S001-v1"
    assert historical.strategy_payload["rule"]["execution"]["capital"]["fee_rate"] == 0.0005
    assert historical.strategy_payload["legacy_identity"] == {
        "version": "baseline_20260903",
        "sha256": "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331",
    }
    assert historical.strategy_payload["execution_review"]["experiment_id"] == "0903_EX01"
    legacy_payload = json.loads(
        (REPO_ROOT / "configs" / "rule_baselines" / "baseline_20260903.json").read_text(
            encoding="utf-8"
        )
    )
    assert historical.strategy_payload["rule"] == legacy_payload
    assert registry.current_qualification("S001", "v2") is Qualification.PAPER_READY


def test_current_strategy_has_research_evidence_and_valid_registry():
    registry = StrategyRegistry(REPO_ROOT / "configs" / "strategies")

    evidence = registry.evidence("S001", "v2")
    result = registry.validate_all()

    assert [(item.phase.value, item.closed_trades) for item in evidence] == [
        ("RESEARCH_BACKTEST", 73)
    ]
    assert evidence[0].source_path == "experiments/0903_EX06/artifacts/evaluation_result.json"
    assert result == {"strategies": 1, "versions": 2, "events": 4, "evidence": 2}
