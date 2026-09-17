"""Immutable execution-channel identities and fail-closed scope validation."""

from __future__ import annotations

from .broker import PaperTradingSafetyError


FUTU_SIMULATE_CN_CHANNEL_ID = "futu_simulate_cn"
LEGACY_FUTU_CHANNEL_ID = "futu"
CHANNEL_RECONCILIATION_ACCOUNT_TYPE = "CHANNEL_RECONCILIATION"
STRATEGY_ACCOUNT_TYPE = "STRATEGY"


def require_futu_simulate_cn(channel_id: object) -> str:
    """Return the only supported broker channel or reject before any I/O."""
    if channel_id != FUTU_SIMULATE_CN_CHANNEL_ID:
        raise PaperTradingSafetyError(
            "PTE only permits the explicit futu_simulate_cn execution channel"
        )
    return FUTU_SIMULATE_CN_CHANNEL_ID


def require_futu_simulate_cn_broker(broker: object) -> str:
    """Ensure a broker adapter declares the precise channel it can execute."""
    return require_futu_simulate_cn(getattr(broker, "channel_id", None))
