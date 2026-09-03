import hashlib
import json

import numpy as np


def comparison_evidence():
    from strategy_evaluator import ReturnMatrixEvidence

    dates = tuple(f"d{index:03d}" for index in range(120))
    incumbent = np.tile([0.001, -0.0008, 0.0005], 40)
    champion = incumbent + 0.00015
    peer = incumbent + 0.00005
    rows = tuple(tuple(map(float, row)) for row in np.column_stack([champion, incumbent, peer]))
    payload = {"dates": dates, "candidate_ids": ("R1102", "S001-v1", "R0539"), "returns": rows}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return ReturnMatrixEvidence(dates, payload["candidate_ids"], rows, digest)


def test_stationary_bootstrap_is_repeatable_and_paired() -> None:
    from strategy_evaluator import paired_stationary_bootstrap

    frame = comparison_evidence()
    values = np.asarray(frame.returns)
    kwargs = {
        "champion_id": "R1102",
        "comparator_id": "S001-v1",
        "repetitions": 500,
        "mean_block_length": 21,
        "seed": 7,
    }

    first = paired_stationary_bootstrap(values[:, 0], values[:, 1], **kwargs)
    second = paired_stationary_bootstrap(values[:, 0], values[:, 1], **kwargs)

    assert first == second
    assert first.cagr.probability_favorable > 0.95
    assert first.max_drawdown.direction == "higher_is_better"
    assert first.cagr.point_difference > 0.0


def test_pairwise_audit_covers_incumbent_peers_and_three_lengths() -> None:
    from strategy_evaluator import audit_pairwise_bootstrap

    rows = audit_pairwise_bootstrap(
        comparison_evidence(), "R1102", "S001-v1", ("R0539",),
        repetitions=100, block_lengths=(21, 10, 42), seed=19,
    )

    assert {(row.comparator_id, row.mean_block_length) for row in rows} == {
        ("S001-v1", 21), ("S001-v1", 10), ("S001-v1", 42),
        ("R0539", 21), ("R0539", 10), ("R0539", 42),
    }


def test_performance_metrics_use_compounded_wealth() -> None:
    from strategy_evaluator import performance_metrics

    result = performance_metrics(np.asarray([0.10, -0.10]))

    assert round(result.max_drawdown, 6) == -0.1
    assert result.cagr < 0.0
    assert result.calmar < 0.0
