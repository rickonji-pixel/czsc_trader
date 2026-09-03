from strategy_evaluator import CandidateProfile, pareto_layers


def profile(candidate_id, values):
    return CandidateProfile(candidate_id, True, True, tuple(zip(("net_cagr", "max_drawdown", "calmar", "profit_factor"), values, strict=True)))


def test_pareto_keeps_tradeoffs_and_assigns_dominated_layers():
    ranked = pareto_layers((profile("a", (2, 0, 1, 1)), profile("b", (0, 2, 1, 1)), profile("c", (-1, -1, 0, 0))))
    assert {item.candidate_id for item in ranked if item.pareto_layer == 1} == {"a", "b"}
    assert next(item for item in ranked if item.candidate_id == "c").pareto_layer == 2
