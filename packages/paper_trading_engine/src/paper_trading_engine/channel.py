"""Immutable execution-channel identities and fail-closed scope validation."""

from __future__ import annotations

from .broker import PaperTradingSafetyError


FUTU_SIMULATE_CN_CHANNEL_ID = "futu_simulate_cn"
FUTU_SIMULATE_US_CHANNEL_ID = "futu_simulate_us"
FUTU_SIMULATE_CHANNEL_IDS = frozenset({
    FUTU_SIMULATE_CN_CHANNEL_ID, FUTU_SIMULATE_US_CHANNEL_ID,
})
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


def futu_simulate_channel_id(market: object) -> str:
    normalized = str(market).strip().upper()
    if normalized == "CN":
        return FUTU_SIMULATE_CN_CHANNEL_ID
    if normalized == "US":
        return FUTU_SIMULATE_US_CHANNEL_ID
    raise PaperTradingSafetyError(f"unsupported Futu simulation market: {market}")


def require_futu_simulate_channel(channel_id: object) -> str:
    if channel_id not in FUTU_SIMULATE_CHANNEL_IDS:
        raise PaperTradingSafetyError(
            "PTE requires an explicit futu_simulate_cn or futu_simulate_us channel"
        )
    return str(channel_id)


def require_futu_simulate_broker(broker: object, expected_channel_id: str | None = None) -> str:
    channel_id = require_futu_simulate_channel(getattr(broker, "channel_id", None))
    if expected_channel_id is not None and channel_id != expected_channel_id:
        raise PaperTradingSafetyError("broker channel differs from PTE runtime")
    return channel_id
