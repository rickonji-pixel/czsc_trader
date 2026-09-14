"""Auditable feature-mining adapters used by strategy research."""

from .tsfresh_adapter import (
    TsfreshExtractionResult,
    TsfreshFeatureSpec,
    TsfreshIntegrationError,
    TsfreshScreenResult,
    extract_causal_rolling_features,
    screen_relevant_features,
)

__all__ = [
    "TsfreshExtractionResult",
    "TsfreshFeatureSpec",
    "TsfreshIntegrationError",
    "TsfreshScreenResult",
    "extract_causal_rolling_features",
    "screen_relevant_features",
]
