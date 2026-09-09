from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.signal_hypotheses import (
    build_review_universe,
    resolve_semantic_reviews,
)


def test_hypothesis_review_enforces_frozen_mechanical_rules() -> None:
    rows = []
    for state_id, state, means, consistency in (
        ("S-A", "入场", (0.01, 0.02, 0.03), 0.75),
        ("S-B", "重复", (0.01, 0.02, 0.03), 0.75),
        ("S-C", "翻转", (0.01, -0.02, 0.03), 0.75),
    ):
        for horizon, mean in zip((3, 5, 10), means):
            rows.append({
                "state_id": state_id,
                "frequency": "daily",
                "name": f"signal_{state_id}",
                "namespace": "signal",
                "state_primary": state,
                "horizon": horizon,
                "evidence_quality": "OK",
                "year_sign_consistency": consistency,
                "net_return_mean": mean,
                "event_count": 20,
                "coverage_years": 4,
                "standardized_effect": mean * 10,
                "mfe_mean": 0.04,
                "mae_mean": -0.02,
            })
    metrics = pd.DataFrame(rows)
    redundancy = {
        "duplicate_groups": [{
            "members": [
                {"state_id": "S-A", "frequency": "daily", "name": "signal_S-A"},
                {"state_id": "S-B", "frequency": "daily", "name": "signal_S-B"},
            ]
        }]
    }
    rules = {
        "horizons": [3, 5, 10],
        "required_evidence_quality": "OK",
        "excluded_primary_states": ["其他"],
        "require_same_mean_direction": True,
        "minimum_year_sign_consistency": 2 / 3,
        "collapse_exact_behavior_duplicates": True,
    }

    universe, counts = build_review_universe(metrics, redundancy, rules)

    assert counts == {
        "eligible_before_exact_deduplication": 2,
        "eligible_after_exact_deduplication": 1,
    }
    assert universe["state_id"].tolist() == ["S-A"]
    selected = [{
        "frequency": "daily",
        "name": "signal_S-A",
        "state": "入场",
        "decision": "SELECT",
        "role": "ENTRY",
        "reason": "语义明确",
    }]
    assert resolve_semantic_reviews(selected, metrics, universe).iloc[0][
        "mechanical_eligible"
    ]

    selected[0]["name"] = "signal_S-C"
    selected[0]["state"] = "翻转"
    with pytest.raises(ValueError, match="mechanical review universe"):
        resolve_semantic_reviews(selected, metrics, universe)
