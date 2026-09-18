from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from czsc_trader.baselines import ResolvedBaseline


@dataclass(frozen=True)
class StrategyIdentity:
    kind: Literal["REGISTERED", "CANDIDATE"]
    reference: str
    source: str


@dataclass(frozen=True)
class StrategySnapshot:
    identity: StrategyIdentity
    source_hash: str
    content_hash: str
    strategy_payload: dict[str, object]
    resolved_rule: ResolvedBaseline | None
    research_start: date | None = None
    research_end: date | None = None
