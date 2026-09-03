from strategy_evaluator import HealthStatus, finalize_evaluation, render_summary
from test_evaluator import health, winner_ranking


def test_report_is_one_page_and_contains_only_decision_fields():
    report = render_summary(finalize_evaluation(winner_ranking(), health(HealthStatus.PASS), "EX"))
    assert "建议冻结" in report
    assert "四项核心指标" in report
    assert report.count("支持理由") == 1
    assert len(report.splitlines()) <= 80
