from __future__ import annotations

from dataclasses import dataclass

from .models import StrategySnapshot


@dataclass(frozen=True)
class BacktestDataContract:
    """Immutable input requirements for replaying one frozen strategy version."""

    market_frequencies: tuple[str, ...] = (
        "daily",
        "30m",
        "weekly",
        "execution_daily",
    )
    intraday_frequencies: tuple[str, ...] = ()
    strategy_support: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "market_frequencies": list(self.market_frequencies),
            "intraday_frequencies": list(self.intraday_frequencies),
            "strategy_support": list(self.strategy_support),
        }


def resolve_backtest_data_contract(snapshot: StrategySnapshot) -> BacktestDataContract:
    """Derive every extra market-data dependency from the frozen rule payload."""

    rule = snapshot.resolved_rule
    if rule.constituent_moneyflow_intraday is not None:
        return BacktestDataContract(intraday_frequencies=("5m",))
    if rule.closing_dislocation_overnight is not None:
        return BacktestDataContract(intraday_frequencies=("1m",))
    if rule.causal_feature_gate is not None:
        return BacktestDataContract(strategy_support=("causal_feature_panel",))
    return BacktestDataContract()
