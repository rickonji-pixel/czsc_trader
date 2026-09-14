"""Thin, causal and auditable adapter around tsfresh.

The adapter deliberately keeps tsfresh on the research side of the FSC boundary.
It generates candidate feature values and relevance evidence; it never writes to
the project catalog or promotes a feature definition automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
from typing import Literal

import numpy as np
import pandas as pd
from tsfresh import extract_features
from tsfresh.feature_extraction import MinimalFCParameters
from tsfresh.feature_selection.relevance import calculate_relevance_table


class TsfreshIntegrationError(ValueError):
    """Input or execution failure with an explicit research-facing meaning."""


@dataclass(frozen=True)
class TsfreshFeatureSpec:
    """Frozen extraction contract for one causal rolling feature matrix."""

    time_column: str
    value_columns: tuple[str, ...]
    lookback: int
    min_periods: int | None = None
    preset: Literal["minimal"] = "minimal"
    n_jobs: int = 0
    max_expanded_rows: int = 1_000_000


@dataclass(frozen=True)
class TsfreshExtractionResult:
    features: pd.DataFrame
    evidence: dict[str, object]


@dataclass(frozen=True)
class TsfreshScreenResult:
    selected_features: pd.DataFrame
    relevance: pd.DataFrame
    evidence: dict[str, object]


def _digest(frame: pd.DataFrame) -> str:
    raw = frame.to_csv(index=True, date_format="%Y-%m-%dT%H:%M:%S.%f%z").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validated_input(frame: pd.DataFrame, spec: TsfreshFeatureSpec) -> pd.DataFrame:
    if spec.lookback < 2:
        raise TsfreshIntegrationError("lookback must be at least 2")
    min_periods = spec.lookback if spec.min_periods is None else spec.min_periods
    if not 2 <= min_periods <= spec.lookback:
        raise TsfreshIntegrationError("min_periods must be between 2 and lookback")
    if spec.preset != "minimal":
        raise TsfreshIntegrationError(f"unsupported tsfresh preset: {spec.preset}")
    if spec.n_jobs < 0:
        raise TsfreshIntegrationError("n_jobs cannot be negative")
    if spec.max_expanded_rows < 1:
        raise TsfreshIntegrationError("max_expanded_rows must be positive")
    if not spec.value_columns or len(set(spec.value_columns)) != len(spec.value_columns):
        raise TsfreshIntegrationError("value_columns must be non-empty and unique")
    required = {spec.time_column, *spec.value_columns}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise TsfreshIntegrationError(f"missing input columns: {missing}")
    if spec.time_column in spec.value_columns:
        raise TsfreshIntegrationError("time_column cannot also be a value column")

    normalized = frame.loc[:, [spec.time_column, *spec.value_columns]].copy()
    try:
        normalized[spec.time_column] = pd.to_datetime(normalized[spec.time_column], errors="raise")
    except (TypeError, ValueError) as exc:
        raise TsfreshIntegrationError(f"invalid time column: {exc}") from exc
    times = normalized[spec.time_column]
    if times.isna().any() or times.duplicated().any() or not times.is_monotonic_increasing:
        raise TsfreshIntegrationError("time column must be non-null, unique and increasing")

    for column in spec.value_columns:
        try:
            normalized[column] = pd.to_numeric(normalized[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise TsfreshIntegrationError(f"value column {column} must be numeric") from exc
        values = normalized[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise TsfreshIntegrationError(f"value column {column} contains null or non-finite values")
    return normalized


def _expanded_row_count(rows: int, lookback: int, min_periods: int) -> int:
    return sum(min(index + 1, lookback) for index in range(min_periods - 1, rows))


def extract_causal_rolling_features(
    frame: pd.DataFrame,
    spec: TsfreshFeatureSpec,
) -> TsfreshExtractionResult:
    """Extract candidate features using only rows at or before each output time.

    The output begins when ``min_periods`` observations exist. Every output row is
    computed from the trailing ``lookback`` observations ending at that same time.
    The default minimal preset intentionally limits feature explosion during the
    integration phase.
    """

    normalized = _validated_input(frame, spec)
    min_periods = spec.lookback if spec.min_periods is None else spec.min_periods
    if len(normalized) < min_periods:
        raise TsfreshIntegrationError(
            f"insufficient rows: need at least {min_periods}, received {len(normalized)}"
        )
    expanded_rows = _expanded_row_count(len(normalized), spec.lookback, min_periods)
    if expanded_rows > spec.max_expanded_rows:
        raise TsfreshIntegrationError(
            "causal rolling expansion exceeds safety limit: "
            f"required={expanded_rows}, limit={spec.max_expanded_rows}"
        )

    windows: list[pd.DataFrame] = []
    endpoints: list[int] = []
    for endpoint in range(min_periods - 1, len(normalized)):
        start = max(0, endpoint - spec.lookback + 1)
        window = normalized.iloc[start : endpoint + 1].copy()
        window.insert(0, "__sample_id", endpoint)
        windows.append(window)
        endpoints.append(endpoint)
    expanded = pd.concat(windows, ignore_index=True)

    try:
        extracted = extract_features(
            expanded,
            column_id="__sample_id",
            column_sort=spec.time_column,
            default_fc_parameters=MinimalFCParameters(),
            kind_to_fc_parameters={column: MinimalFCParameters() for column in spec.value_columns},
            n_jobs=spec.n_jobs,
            disable_progressbar=True,
        )
    except Exception as exc:
        raise TsfreshIntegrationError(f"tsfresh extraction failed: {exc}") from exc

    extracted = extracted.reindex(endpoints)
    extracted.index = pd.DatetimeIndex(
        normalized.iloc[endpoints][spec.time_column], name=spec.time_column
    )
    extracted = extracted.rename(columns=lambda name: f"tsfresh__{name}").sort_index(axis=1)
    values = extracted.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise TsfreshIntegrationError("tsfresh produced null or non-finite feature values")

    evidence: dict[str, object] = {
        "provider": "tsfresh",
        "provider_version": version("tsfresh"),
        "preset": spec.preset,
        "evidence_scope": "DEVELOPMENT_ONLY",
        "causality_contract": "TRAILING_WINDOW_ENDS_AT_FEATURE_TIME",
        "time_column": spec.time_column,
        "value_columns": list(spec.value_columns),
        "lookback": spec.lookback,
        "min_periods": min_periods,
        "input_rows": len(normalized),
        "expanded_rows": expanded_rows,
        "output_rows": len(extracted),
        "feature_count": extracted.shape[1],
        "first_feature_time": extracted.index.min().isoformat(),
        "last_feature_time": extracted.index.max().isoformat(),
        "input_digest": _digest(normalized.set_index(spec.time_column)),
        "feature_digest": _digest(extracted),
    }
    return TsfreshExtractionResult(extracted, evidence)


def screen_relevant_features(
    features: pd.DataFrame,
    target: pd.Series,
    *,
    fdr_level: float = 0.05,
    ml_task: Literal["auto", "classification", "regression"] = "auto",
    n_jobs: int = 0,
) -> TsfreshScreenResult:
    """Run tsfresh's univariate FDR screen and return the full audit table.

    This is development evidence. Autocorrelated market observations do not become
    independent merely because the statistical test completed successfully.
    """

    if features.empty or features.columns.empty:
        raise TsfreshIntegrationError("features must be a non-empty matrix")
    if not features.index.is_unique or not target.index.is_unique:
        raise TsfreshIntegrationError("feature and target indices must be unique")
    if not features.index.equals(target.index):
        raise TsfreshIntegrationError("feature and target indices must match exactly")
    if features.columns.duplicated().any():
        raise TsfreshIntegrationError("feature names must be unique")
    if not 0 < fdr_level <= 1:
        raise TsfreshIntegrationError("fdr_level must be in (0, 1]")
    if ml_task not in {"auto", "classification", "regression"}:
        raise TsfreshIntegrationError(f"unsupported ml_task: {ml_task}")
    if n_jobs < 0:
        raise TsfreshIntegrationError("n_jobs cannot be negative")
    if target.isna().any() or target.nunique(dropna=True) < 2:
        raise TsfreshIntegrationError("target must contain at least two non-null values")
    if not np.isfinite(features.to_numpy(dtype=float)).all():
        raise TsfreshIntegrationError("features contain null or non-finite values")

    try:
        relevance = calculate_relevance_table(
            features,
            target,
            ml_task=ml_task,
            fdr_level=fdr_level,
            hypotheses_independent=False,
            n_jobs=n_jobs,
            show_warnings=False,
        ).reset_index(drop=True)
        relevance = relevance.sort_values(
            ["relevant", "p_value", "feature"], ascending=[False, True, True]
        )
    except Exception as exc:
        raise TsfreshIntegrationError(f"tsfresh relevance screen failed: {exc}") from exc
    selected_names = relevance.loc[relevance["relevant"], "feature"].tolist()
    selected = features.loc[:, selected_names].copy()
    evidence: dict[str, object] = {
        "provider": "tsfresh",
        "provider_version": version("tsfresh"),
        "method": "UNIVARIATE_BENJAMINI_HOCHBERG",
        "evidence_scope": "DEVELOPMENT_ONLY",
        "ml_task": ml_task,
        "fdr_level": fdr_level,
        "hypotheses_independent": False,
        "sample_count": len(features),
        "input_feature_count": features.shape[1],
        "selected_feature_count": selected.shape[1],
        "selected_features": selected_names,
        "feature_digest": _digest(features),
        "target_digest": _digest(target.to_frame(name=target.name or "target")),
    }
    return TsfreshScreenResult(selected, relevance.reset_index(drop=True), evidence)
