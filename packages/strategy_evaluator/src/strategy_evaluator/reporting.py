from .models import Decision, EvaluationResult


_DECISION_LABELS = {
    Decision.RECOMMEND_FREEZE: "建议冻结",
    Decision.KEEP_INCUMBENT: "保留当前策略",
    Decision.INSUFFICIENT_EVIDENCE: "证据不足",
}


def render_summary(result: EvaluationResult) -> str:
    candidate = result.recommended_candidate_id or "无"
    lines = [
        f"# 策略评估摘要：{result.experiment_id or '未命名实验'}",
        "",
        f"- 结论：{_DECISION_LABELS[result.decision]}",
        f"- 当前策略：{result.incumbent_id}",
        f"- 推荐候选：{candidate}",
        "",
        "## 四项核心指标",
        "",
        "| 候选 | 年化收益 | 最大回撤 | 卡玛比率 | 盈亏因子 |",
        "|---|---:|---:|---:|---:|",
    ]
    for profile in result.ranking.profiles:
        scores = dict(profile.worst_scores)
        lines.append(f"| {profile.candidate_id} | {scores.get('net_cagr', float('nan')):.2f} | {scores.get('max_drawdown', float('nan')):.2f} | {scores.get('calmar', float('nan')):.2f} | {scores.get('profit_factor', float('nan')):.2f} |")
    if result.audit is not None:
        risk = "无" if result.audit.risk_label is None else result.audit.risk_label.value
        lines.extend([
            "",
            "## 统计稳健性审计",
            "",
            f"- 审计完整性：{result.audit.status.value}",
            f"- 统计风险：{risk}",
        ])
        if result.audit.search_bias is not None:
            lines.append(f"- Sharpe-PBO：{result.audit.search_bias.pbo:.4f}")
        if result.audit.dsr is not None:
            lines.append(
                f"- DSR：原始试验数 {result.audit.dsr.raw.probability:.4f}；"
                f"有效试验数 {result.audit.dsr.effective.probability:.4f}"
            )
        if result.audit.direction_flags:
            flags = "；".join(f"{name}={value}" for name, value in result.audit.direction_flags)
            lines.append(f"- 证据方向：{flags}")
        lines.append("- 统计审计完成不等于统计优势已经得到证明。")
    lines.extend(["", "## 支持理由", "", ", ".join(result.reason_codes), "", "最终是否冻结由人工确认。"])
    return "\n".join(lines) + "\n"
