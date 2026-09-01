from __future__ import annotations

import importlib
import json
from pathlib import Path

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _module():
    return importlib.import_module("czsc_trader.regime_tournament")


def test_metric_score_uses_one_half_zero_with_tolerance() -> None:
    module = _module()

    assert module.metric_score(1.2, 1.0, tolerance=1e-12) == 1.0
    assert module.metric_score(1.0 + 5e-13, 1.0, tolerance=1e-12) == 0.5
    assert module.metric_score(0.9, 1.0, tolerance=1e-12) == 0.0


def test_total_score_ranking_uses_dense_ties_and_not_candidate_id() -> None:
    module = _module()
    rows = pd.DataFrame(
        [
            {"candidate_id": 293, "total_score": 7.0},
            {"candidate_id": 143, "total_score": 8.5},
            {"candidate_id": 125, "total_score": 7.0},
            {"candidate_id": 400, "total_score": 4.5},
        ]
    )

    ranked = module.rank_total_scores(rows)

    assert ranked[["candidate_id", "rank"]].to_dict("records") == [
        {"candidate_id": 143, "rank": 1},
        {"candidate_id": 125, "rank": 2},
        {"candidate_id": 293, "rank": 2},
        {"candidate_id": 400, "rank": 3},
    ]


def test_protocol_and_source_candidate_identity_are_frozen() -> None:
    runner = importlib.import_module("czsc_trader.regime_tournament_runner")
    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0901_EX21"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )
    runner.validate_regime_tournament_protocol(protocol)

    changed = dict(protocol)
    changed["candidate_ids"] = [143]
    with pytest.raises(ValueError, match="candidate_ids"):
        runner.validate_regime_tournament_protocol(changed)

    source = pd.DataFrame(
        {
            "candidate_id": [125, 143, 275, 293, 400, 418, 999],
            "pass": [True, True, True, True, True, True, False],
            "trend_trend_multiplier": [1.0] * 7,
            "trend_volume_multiplier": [1.0] * 7,
            "range_trend_multiplier": [1.0] * 7,
            "range_volume_multiplier": [1.0] * 7,
        }
    )
    selected = runner.select_source_candidates(source, protocol["candidate_ids"])
    assert selected["candidate_id"].tolist() == [125, 143, 275, 293, 400, 418]
