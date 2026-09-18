from __future__ import annotations

from pathlib import Path

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import strategy_source as strategy_source_module
from czsc_trader.backtesting.strategy_source import (
    resolve_candidate_snapshot,
    resolve_registered_strategy,
)


def test_backtest_strategy_sources_preserve_identity_and_rule(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    registered = resolve_registered_strategy(context, "S001", "v1")
    candidate_hash = "c" * 64
    candidate = resolve_candidate_snapshot(
        context,
        "0904_EX04:R1102",
        registered.strategy_payload,
        candidate_hash,
        "experiments/0904_EX04",
    )

    assert registered.identity.kind == "REGISTERED"
    assert registered.identity.reference == "S001-v1"
    assert candidate.identity.kind == "CANDIDATE"
    assert candidate.identity.reference == "0904_EX04:R1102"
    assert registered.source_hash == "ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62"
    assert candidate.source_hash == candidate_hash
    assert registered.content_hash
    assert candidate.content_hash
    assert registered.resolved_rule is None
    assert candidate.resolved_rule is not None
    assert registered.strategy_payload == candidate.strategy_payload


def test_registered_strategy_snapshot_does_not_require_tdr_rule_resolution(
    functional_repo: Path, monkeypatch
) -> None:
    context = RepositoryContext.discover(functional_repo)

    def fail_legacy_resolution(*args, **kwargs):
        raise AssertionError("registered SRT must not use the TDR legacy rule resolver")

    monkeypatch.setattr(
        strategy_source_module, "resolve_strategy_payload", fail_legacy_resolution
    )
    registered = resolve_registered_strategy(context, "S001", "v1")

    assert registered.identity.reference == "S001-v1"
    assert registered.resolved_rule is None
