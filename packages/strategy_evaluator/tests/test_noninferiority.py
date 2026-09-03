from dataclasses import replace

from strategy_evaluator import OPC_V1, OPC_V2, MarginSet, MetricObservation, MetricStatus, compare_observation


def obs(**changes):
    base = MetricObservation("x", "full", "standard", "FORMAL", 0.10, 0.5, -0.10, 1.0, MetricStatus.VALID, 2.0, MetricStatus.VALID, 20)
    return replace(base, **changes)


def comparison(items, metric):
    return next(item for item in items if item.metric == metric)


def test_candidate_fails_when_positive_cagr_retention_is_below_ninety_percent():
    item = comparison(compare_observation(obs(net_cagr=0.089), obs(net_cagr=0.10), MarginSet()), "net_cagr")
    assert item.passed is False
    assert item.normalized_score < -1


def test_drawdown_uses_the_stricter_absolute_and_relative_boundary():
    item = comparison(compare_observation(obs(max_drawdown=-0.116), obs(max_drawdown=-0.10), MarginSet()), "max_drawdown")
    assert item.passed is False


def test_low_sample_profit_factor_cannot_prove_superiority():
    item = comparison(compare_observation(obs(profit_factor=9, closed_trades=4, profit_factor_status=MetricStatus.LOW_SAMPLE), obs(), MarginSet()), "profit_factor")
    assert item.comparable is False
    assert item.passed is False


def test_v2_accepts_improved_negative_calmar_while_v1_requires_positive():
    candidate = obs(calmar=-0.05)
    incumbent = obs(calmar=-0.10)
    v1 = comparison(compare_observation(candidate, incumbent, OPC_V1.margins), "calmar")
    v2 = comparison(compare_observation(candidate, incumbent, OPC_V2.margins), "calmar")
    assert v1.passed is False
    assert v2.passed is True
    assert v2.normalized_score > 0
