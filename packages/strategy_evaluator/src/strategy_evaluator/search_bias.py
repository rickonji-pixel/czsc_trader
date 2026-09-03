from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np
from scipy.stats import norm

from .audit_models import ReturnMatrixEvidence
from .models import Record


@dataclass(frozen=True)
class CscvSplit(Record):
    split_id: int
    training_blocks: tuple[int, ...]
    validation_blocks: tuple[int, ...]
    selected_candidate: str
    training_sharpe: float
    validation_sharpe: float
    validation_rank: int
    validation_candidate_count: int
    validation_percentile: float
    logit: float


@dataclass(frozen=True)
class SearchBiasResult(Record):
    block_count: int
    candidate_count: int
    pbo: float
    median_validation_percentile: float
    median_logit: float
    selection_frequency: tuple[tuple[str, int], ...]
    splits: tuple[CscvSplit, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "block_count": self.block_count,
            "candidate_count": self.candidate_count,
            "pbo": self.pbo,
            "median_validation_percentile": self.median_validation_percentile,
            "median_logit": self.median_logit,
            "selection_frequency": dict(self.selection_frequency),
            "splits": [item.to_dict() for item in self.splits],
        }


@dataclass(frozen=True)
class DsrEstimate(Record):
    observations: int
    trial_count: float
    observed_sharpe: float
    trial_sharpe_mean: float
    trial_sharpe_std: float
    expected_max_sharpe: float
    skew: float
    pearson_kurtosis: float
    test_statistic: float
    probability: float


@dataclass(frozen=True)
class DsrBundle(Record):
    raw: DsrEstimate
    effective: DsrEstimate


def _matrix(evidence: ReturnMatrixEvidence) -> np.ndarray:
    values = np.asarray(evidence.returns, dtype=float)
    expected = (len(evidence.dates), len(evidence.candidate_ids))
    if values.shape != expected or values.ndim != 2:
        raise ValueError(f"return matrix shape must be {expected}")
    if len(evidence.dates) == 0 or len(evidence.candidate_ids) < 2:
        raise ValueError("return matrix requires rows and at least two candidates")
    if len(set(evidence.dates)) != len(evidence.dates):
        raise ValueError("return dates must be unique")
    if len(set(evidence.candidate_ids)) != len(evidence.candidate_ids):
        raise ValueError("candidate IDs must be unique")
    if not np.isfinite(values).all():
        raise ValueError("candidate returns must all be finite")
    return values


def annualized_sharpe(values: np.ndarray) -> float:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if array.size < 2:
        return float("nan")
    volatility = float(np.std(array, ddof=1))
    if volatility <= 0.0:
        mean = float(np.mean(array))
        return float(np.sign(mean) * np.inf) if mean else 0.0
    return float(np.sqrt(252.0) * np.mean(array) / volatility)


def _sharpes(values: np.ndarray) -> np.ndarray:
    means = np.mean(values, axis=0)
    volatility = np.std(values, axis=0, ddof=1)
    result = np.divide(
        np.sqrt(252.0) * means,
        volatility,
        out=np.full_like(means, np.nan, dtype=float),
        where=volatility > 0.0,
    )
    zero = volatility <= 0.0
    result[zero & (means == 0.0)] = 0.0
    return result


def _identity_key(value: str) -> tuple[int, int | str]:
    try:
        return 0, int(value)
    except ValueError:
        return 1, value


def cscv_pbo(evidence: ReturnMatrixEvidence, block_count: int = 10) -> SearchBiasResult:
    values = _matrix(evidence)
    if block_count < 2 or block_count % 2 or len(values) < block_count:
        raise ValueError("block_count must be even and no greater than observations")
    blocks = tuple(np.asarray(block, dtype=int) for block in np.array_split(np.arange(len(values)), block_count))
    candidate_count = values.shape[1]
    lower_clip = 0.5 / candidate_count
    upper_clip = 1.0 - lower_clip
    splits: list[CscvSplit] = []
    frequency = {candidate_id: 0 for candidate_id in evidence.candidate_ids}
    for split_id, training_blocks in enumerate(combinations(range(block_count), block_count // 2)):
        training_set = set(training_blocks)
        validation_blocks = tuple(index for index in range(block_count) if index not in training_set)
        training_rows = np.concatenate([blocks[index] for index in training_blocks])
        validation_rows = np.concatenate([blocks[index] for index in validation_blocks])
        training = _sharpes(values[training_rows])
        finite_training = np.flatnonzero(np.isfinite(training))
        if finite_training.size < 2:
            raise ValueError(f"split {split_id} has fewer than two finite training sharpes")
        selected_index = sorted(
            finite_training,
            key=lambda index: (-float(training[index]), _identity_key(evidence.candidate_ids[index])),
        )[0]
        selected = evidence.candidate_ids[selected_index]
        validation = _sharpes(values[validation_rows])
        finite_validation = np.flatnonzero(np.isfinite(validation))
        if finite_validation.size < 2 or selected_index not in finite_validation:
            raise ValueError(f"split {split_id} has invalid validation sharpes")
        order = sorted(
            finite_validation,
            key=lambda index: (-float(validation[index]), _identity_key(evidence.candidate_ids[index])),
        )
        rank = order.index(selected_index) + 1
        percentile = float((len(order) - rank) / (len(order) - 1))
        clipped = float(np.clip(percentile, lower_clip, upper_clip))
        frequency[selected] += 1
        splits.append(CscvSplit(
            split_id, tuple(training_blocks), validation_blocks, selected,
            float(training[selected_index]), float(validation[selected_index]), rank, len(order),
            percentile, float(np.log(clipped / (1.0 - clipped))),
        ))
    logits = np.asarray([item.logit for item in splits])
    percentiles = np.asarray([item.validation_percentile for item in splits])
    return SearchBiasResult(
        block_count, candidate_count, float(np.mean(logits < 0.0)),
        float(np.median(percentiles)), float(np.median(logits)),
        tuple(sorted(frequency.items(), key=lambda item: _identity_key(item[0]))), tuple(splits),
    )


def effective_trial_count(values: np.ndarray) -> float:
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 2 or matrix.shape[1] < 2:
        raise ValueError("effective trial count requires a two-dimensional candidate matrix")
    if not np.isfinite(matrix).all() or np.any(np.std(matrix, axis=0, ddof=1) <= 0.0):
        raise ValueError("candidate returns must have finite positive dispersion")
    eigenvalues = np.linalg.eigvalsh(np.corrcoef(matrix, rowvar=False))
    eigenvalues = np.clip(eigenvalues, 0.0, None)
    denominator = float(np.square(eigenvalues).sum())
    if denominator <= 0.0:
        raise ValueError("candidate correlation has no positive eigenvalues")
    return float(eigenvalues.sum() ** 2 / denominator)


def deflated_sharpe_ratio(
    selected_returns: np.ndarray,
    trial_sharpes: np.ndarray,
    trial_count: float,
) -> DsrEstimate:
    values = np.asarray(selected_returns, dtype=float)
    trials = np.asarray(trial_sharpes, dtype=float)
    values = values[np.isfinite(values)]
    trials = trials[np.isfinite(trials)]
    if values.size < 3 or trials.size < 2 or trial_count < 1.0:
        raise ValueError("DSR requires returns, dispersed trials, and trial_count >= 1")
    trial_std = float(np.std(trials, ddof=1))
    if trial_std <= 0.0:
        raise ValueError("trial sharpes must have positive dispersion")
    trial_mean = float(np.mean(trials))
    if trial_count <= 1.0 + 1e-12:
        expected_max = trial_mean
    else:
        gamma = 0.5772156649015329
        expected_standard_max = (
            (1.0 - gamma) * norm.ppf(1.0 - 1.0 / trial_count)
            + gamma * norm.ppf(1.0 - 1.0 / (trial_count * np.e))
        )
        expected_max = float(trial_mean + trial_std * expected_standard_max)
    observed = annualized_sharpe(values)
    daily_observed = observed / np.sqrt(252.0)
    daily_benchmark = expected_max / np.sqrt(252.0)
    centered = values - np.mean(values)
    standard = np.std(values, ddof=1)
    skew = float(np.mean((centered / standard) ** 3))
    pearson_kurtosis = float(np.mean((centered / standard) ** 4))
    variance = 1.0 - skew * daily_observed + ((pearson_kurtosis - 1.0) / 4.0) * daily_observed**2
    if not np.isfinite(variance) or variance <= 0.0:
        raise ValueError("DSR variance adjustment must be positive and finite")
    statistic = float((daily_observed - daily_benchmark) * np.sqrt(len(values) - 1.0) / np.sqrt(variance))
    return DsrEstimate(
        len(values), float(trial_count), observed, trial_mean, trial_std, expected_max,
        skew, pearson_kurtosis, statistic, float(norm.cdf(statistic)),
    )


def calculate_dsr_bundle(
    selected_returns: np.ndarray,
    trial_sharpes: np.ndarray,
    *,
    raw_count: int,
    effective_count: float,
) -> DsrBundle:
    return DsrBundle(
        deflated_sharpe_ratio(selected_returns, trial_sharpes, float(raw_count)),
        deflated_sharpe_ratio(selected_returns, trial_sharpes, effective_count),
    )
