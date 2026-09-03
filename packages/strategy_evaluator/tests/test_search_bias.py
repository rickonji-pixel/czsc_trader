import hashlib
import json

import numpy as np
import pytest


def evidence(values: np.ndarray, ids: tuple[str, ...]):
    from strategy_evaluator import ReturnMatrixEvidence

    dates = tuple(f"2025-01-{day:02d}" for day in range(1, len(values) + 1))
    rows = tuple(tuple(float(item) for item in row) for row in values)
    payload = {"dates": dates, "candidate_ids": ids, "returns": rows}
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return ReturnMatrixEvidence(dates, ids, rows, digest)


def test_cscv_uses_all_252_complement_splits() -> None:
    from strategy_evaluator import cscv_pbo

    values = np.column_stack(
        [
            np.tile([0.01, -0.002], 10),
            np.r_[np.full(10, 0.02), np.full(10, -0.02)],
            np.r_[np.full(10, -0.02), np.full(10, 0.02)],
        ]
    )

    result = cscv_pbo(evidence(values, ("candidate_0", "candidate_1", "candidate_2")))

    assert len(result.splits) == 252
    assert 0.0 <= result.pbo <= 1.0
    assert sum(count for _, count in result.selection_frequency) == 252
    assert all(0.0 <= split.validation_percentile <= 1.0 for split in result.splits)


def test_cscv_tie_breaks_by_numeric_candidate_identity() -> None:
    from strategy_evaluator import cscv_pbo

    values = np.column_stack([np.tile([0.01, -0.002], 10)] * 2)

    result = cscv_pbo(evidence(values, ("10", "2")))

    assert {split.selected_candidate for split in result.splits} == {"2"}


def test_effective_trials_collapse_for_correlated_candidates() -> None:
    from strategy_evaluator import effective_trial_count

    base = np.sin(np.arange(100) / 7.0) * 0.01
    correlated = np.column_stack([base, base * 1.01, base * 0.99])
    independent = np.column_stack(
        [base, np.cos(np.arange(100) / 5.0) * 0.01, np.sign(np.sin(np.arange(100))) * 0.01]
    )

    assert effective_trial_count(correlated) == pytest.approx(1.0)
    assert effective_trial_count(independent) > 2.0


def test_dsr_bundle_reports_raw_and_effective_trial_counts() -> None:
    from strategy_evaluator import calculate_dsr_bundle

    champion = np.tile([0.0012, -0.0009], 250)
    trial_sharpes = np.linspace(0.1, 1.1, 100)

    result = calculate_dsr_bundle(
        champion, trial_sharpes, raw_count=1_187, effective_count=5.2
    )

    assert result.raw.trial_count == 1_187
    assert result.effective.trial_count == pytest.approx(5.2)
    assert 0.0 <= result.raw.probability <= 1.0
    assert result.effective.probability > result.raw.probability
