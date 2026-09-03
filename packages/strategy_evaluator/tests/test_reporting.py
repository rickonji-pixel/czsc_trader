from strategy_evaluator import HealthStatus, finalize_evaluation, render_summary
from test_evaluator import health, winner_ranking


def test_report_is_one_page_and_contains_only_decision_fields():
    report = render_summary(finalize_evaluation(winner_ranking(), health(HealthStatus.PASS), "EX"))
    assert "建议冻结" in report
    assert "四项核心指标" in report
    assert report.count("支持理由") == 1
    assert len(report.splitlines()) <= 80


def test_opc_v3_report_separates_complete_audit_from_statistical_strength():
    from strategy_evaluator import audit_provisional_champion
    from test_champion_audit import complete_request, ranking

    audit = audit_provisional_champion(complete_request(champion_edge=-0.0002))
    result = finalize_evaluation(
        ranking(), None, "0903_EX06", audit=audit, standard_version="opc-v3",
    )
    report = render_summary(result)

    assert "统计稳健性审计" in report
    assert "审计完整性：PASS" in report
    assert "统计风险：WEAK" in report
    assert "统计审计完成不等于统计优势已经得到证明" in report
