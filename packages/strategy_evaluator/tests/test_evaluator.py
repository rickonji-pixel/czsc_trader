from dataclasses import replace

from strategy_evaluator import (
    CandidateDescriptor,
    EvaluationProtocol,
    MetricObservation,
    MetricStatus,
    ShortlistResult,
    rank_candidates,
    screen_candidates,
)
from test_models import PROTOCOL


def observation(candidate_id, window, **changes):
    base = MetricObservation(candidate_id, window, "standard", "FORMAL", 0.10, 0.5, -0.10, 1.0, MetricStatus.VALID, 2.0, MetricStatus.VALID, 20, 2.0, 0.01, (("range_return", -0.1),))
    return replace(base, **changes)


def test_bad_ytd_eliminates_strong_long_term_candidate():
    protocol = EvaluationProtocol.from_dict(PROTOCOL)
    candidates = (
        CandidateDescriptor("S001-v1", "a", "b" * 64, True, "h0"),
        CandidateDescriptor("1010", "c", "b" * 64, False, "h1"),
    )
    observations = (
        observation("S001-v1", "full"), observation("S001-v1", "2026_ytd"),
        observation("1010", "full", net_cagr=0.15, objective_values=(("range_return", 0.0),)),
        observation("1010", "2026_ytd", net_cagr=0.05, objective_values=(("range_return", 0.0),)),
    )
    ranking = rank_candidates(protocol, ShortlistResult(("1010",), ()), observations, candidates)
    challenger = next(item for item in ranking.profiles if item.candidate_id == "1010")
    assert challenger.eligible is False
    assert "NONINFERIORITY_NET_CAGR_2026_YTD" in challenger.reason_codes


def test_screening_deduplicates_behavior_and_preserves_incumbent():
    protocol = EvaluationProtocol.from_dict(PROTOCOL)
    candidates = (
        CandidateDescriptor("S001-v1", "a", "b" * 64, True, "h0"),
        CandidateDescriptor("c1", "c", "b" * 64, False, "same"),
        CandidateDescriptor("c2", "d", "b" * 64, False, "same"),
    )
    observations = tuple(observation(cid, window) for cid in ("S001-v1", "c1", "c2") for window in ("full", "2026_ytd"))
    result = screen_candidates(protocol, candidates, observations)
    assert result.candidate_ids == ("c1",)
    assert result.rejected_ids == ("c2",)
