from __future__ import annotations

from pathlib import Path

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.strategy_source import resolve_registered_strategy


def test_registered_strategy_snapshot_does_not_require_tdr_rule_resolution(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    registered = resolve_registered_strategy(context, "S001", "v1")
    assert registered.identity.kind == "REGISTERED"
    assert registered.identity.reference == "S001-v1"
    assert registered.source_hash == "ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62"
    assert registered.content_hash
    assert registered.strategy_payload
