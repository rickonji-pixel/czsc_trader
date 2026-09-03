from dataclasses import replace

from strategy_evaluator import (
    CandidateDescriptor,
    EvaluationProtocol,
    MetricObservation,
    MetricStatus,
    ShortlistResult,
    rank_candidates,
    screen_candidates,
    CandidateProfile,
    HealthEvidence,
    HealthStatus,
    RankingResult,
    Decision,
    finalize_evaluation,
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


def winner_ranking():
    profile = CandidateProfile("c1", True, True, (("net_cagr", 1.0), ("max_drawdown", 1.0), ("calmar", 1.0), ("profit_factor", 1.0)), 1, 1.0)
    return RankingResult("S001-v1", (profile,), "c1", ("c1",))


def health(status):
    return HealthEvidence("c1", status, status, status, status, status)


def test_only_a_unique_healthy_champion_recommends_freeze():
    result = finalize_evaluation(winner_ranking(), health(HealthStatus.PASS), "EX")
    assert result.decision is Decision.RECOMMEND_FREEZE
    assert result.recommended_candidate_id == "c1"


def test_failed_health_keeps_incumbent_and_missing_health_is_insufficient():
    assert finalize_evaluation(winner_ranking(), health(HealthStatus.FAIL)).decision is Decision.KEEP_INCUMBENT
    assert finalize_evaluation(winner_ranking(), health(HealthStatus.INSUFFICIENT)).decision is Decision.INSUFFICIENT_EVIDENCE
