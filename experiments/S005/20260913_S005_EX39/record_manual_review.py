from __future__ import annotations

from pathlib import Path

import pandas as pd


# Human judgements made by Codex after reviewing the fixed chronological pilot.
# This is an auditable record of decisions, not a reusable semantic classifier.
EVENTS: dict[str, list[tuple[str, str, str, str, str]]] = {
    "EX39-0009": [("寒武纪", "CAPITAL_ALLOCATION", "AMBIGUOUS", "员工限制性股票完成归属登记", "寒武纪|CAPITAL_ALLOCATION|20260817")],
    "EX39-0017": [("上海合晶", "CAPITAL_ALLOCATION", "NEGATIVE", "董事及高级管理人员披露减持计划", "上海合晶|CAPITAL_ALLOCATION|20260818")],
    "EX39-0033": [
        ("瑞芯微", "EARNINGS_CHANGE", "POSITIVE", "上半年收入和利润增长", "瑞芯微|EARNINGS_CHANGE|2026H1"),
        ("半导体供应链", "SUPPLY_DISRUPTION", "NEGATIVE", "存储和多类上游材料供应承压", "半导体供应链|SUPPLY_DISRUPTION|2026H1"),
    ],
    "EX39-0035": [
        ("复旦微电", "EARNINGS_CHANGE", "POSITIVE", "上半年利润同比大幅增长", "复旦微电|EARNINGS_CHANGE|2026H1"),
        ("复旦微电", "PRODUCT_RELEASE", "POSITIVE", "发布FPGA IP、解决方案及Agent开发平台", "复旦微电|PRODUCT_RELEASE|2026H1")
    ],
    "EX39-0037": [("PCB产业链", "INDUSTRY_DATA", "POSITIVE", "多家产业链公司业绩增长且高端产品供需偏紧", "PCB产业链|INDUSTRY_DATA|2026H1")],
    "EX39-0043": [("CMP材料", "INDUSTRY_DATA", "POSITIVE", "先进制程和存储升级增加CMP工序及材料需求", "CMP材料|INDUSTRY_DATA|20260818")],
    "EX39-0093": [
        ("芯原股份", "ORDER_DEMAND", "POSITIVE", "AI算力订单占新签订单九成", "芯原股份|ORDER_DEMAND|20260817"),
        ("芯原股份", "EARNINGS_CHANGE", "NEGATIVE", "上半年收入增长但仍持续亏损", "芯原股份|EARNINGS_CHANGE|2026H1"),
    ],
    "EX39-0098": [("磷化铟基板", "PRICE_CHANGE", "POSITIVE", "供不应求推动第四季度价格继续上涨", "磷化铟基板|PRICE_CHANGE|2026Q4")],
    "EX39-0103": [
        ("海光信息", "EARNINGS_CHANGE", "POSITIVE", "上半年收入和利润创同期新高", "海光信息|EARNINGS_CHANGE|2026H1"),
        ("海光信息", "CAPITAL_ALLOCATION", "AMBIGUOUS", "为保障供应扩大存货和预付款", "海光信息|CAPITAL_ALLOCATION|2026H1"),
    ],
    "EX39-0105": [
        ("富乐德", "CAPEX", "POSITIVE", "拟募资建设半导体洗净、修复和载板项目", "富乐德|CAPEX|20260818"),
        ("富乐德", "EARNINGS_CHANGE", "POSITIVE", "上半年利润预计增长", "富乐德|EARNINGS_CHANGE|2026H1"),
    ],
    "EX39-0107": [("中际旭创", "M_AND_A", "POSITIVE", "入股散热和PCB企业以强化AI光模块供应链", "中际旭创|M_AND_A|20260818")],
    "EX39-0155": [
        ("芯原股份", "ORDER_DEMAND", "POSITIVE", "AI算力订单占新签订单九成", "芯原股份|ORDER_DEMAND|20260817"),
        ("芯原股份", "EARNINGS_CHANGE", "NEGATIVE", "上半年收入增长但仍持续亏损", "芯原股份|EARNINGS_CHANGE|2026H1"),
    ],
    "EX39-0158": [
        ("芯原股份", "ORDER_DEMAND", "POSITIVE", "AI算力订单占新签订单九成", "芯原股份|ORDER_DEMAND|20260817"),
        ("芯原股份", "EARNINGS_CHANGE", "NEGATIVE", "上半年收入增长但仍持续亏损", "芯原股份|EARNINGS_CHANGE|2026H1"),
    ],
    "EX39-0178": [
        ("瑞芯微", "EARNINGS_CHANGE", "POSITIVE", "上半年收入和利润增长", "瑞芯微|EARNINGS_CHANGE|2026H1"),
        ("半导体供应链", "SUPPLY_DISRUPTION", "NEGATIVE", "存储和多类上游材料供应承压", "半导体供应链|SUPPLY_DISRUPTION|2026H1"),
    ],
    "EX39-0193": [
        ("芯原股份", "ORDER_DEMAND", "POSITIVE", "AI算力订单占新签订单九成", "芯原股份|ORDER_DEMAND|20260817"),
        ("芯原股份", "EARNINGS_CHANGE", "NEGATIVE", "上半年收入增长但仍持续亏损", "芯原股份|EARNINGS_CHANGE|2026H1"),
    ],
    "EX39-0197": [
        ("芯原股份", "ORDER_DEMAND", "POSITIVE", "AI算力订单占新签订单九成", "芯原股份|ORDER_DEMAND|20260817"),
        ("芯原股份", "EARNINGS_CHANGE", "NEGATIVE", "上半年收入增长但仍持续亏损", "芯原股份|EARNINGS_CHANGE|2026H1"),
    ],
    "EX39-0213": [("远东股份", "CAPEX", "POSITIVE", "AIDC光纤预制棒项目主体封顶并推进设备调试", "远东股份|CAPEX|20260818")],
    "EX39-0249": [
        ("传音控股", "EARNINGS_CHANGE", "POSITIVE", "上半年收入和利润增长", "传音控股|EARNINGS_CHANGE|2026H1"),
        ("传音控股", "CAPITAL_ALLOCATION", "AMBIGUOUS", "应对存储涨价大幅增加库存和采购付款", "传音控股|CAPITAL_ALLOCATION|2026H1"),
    ],
}


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    index = pd.read_csv(experiment / "artifacts/article_index.csv", keep_default_na=False)
    raw = pd.read_csv(repo / ".tmp/research_cache/S005/news/ex39_sina_full_pilot.csv.gz", keep_default_na=False)
    pilot = raw.loc[raw["sample_id"].isin(index["sample_id"])].copy()
    if len(index) != 250 or len(pilot) != 250 or index["sample_id"].duplicated().any():
        raise ValueError("EX39 manual record expects the fixed 250-row pilot")

    labels: list[dict[str, str]] = []
    event_rows: list[dict[str, str]] = []
    for row in pilot.itertuples(index=False):
        relevant = row.sample_id in EVENTS
        labels.append(
            {
                "sample_id": row.sample_id,
                "relevance": "CATALYST_RELEVANT" if relevant else "NOT_CATALYST",
                "review_evidence_excerpt": row.title,
                "review_reason": "DIRECT_SCOPE_FACT" if relevant else "OUTSIDE_S005_NEWS_SCOPE_OR_NO_NEW_FACT",
            }
        )
        for entity, event_type, direction, summary, duplicate_key in EVENTS.get(row.sample_id, []):
            event_rows.append(
                {
                    "event_id": f"EVT-{len(event_rows) + 1:03d}",
                    "sample_id": row.sample_id,
                    "published_at": row.pub_time,
                    "primary_entity": entity,
                    "event_type": event_type,
                    "direction": direction,
                    "event_summary": summary,
                    "evidence_excerpt": row.title,
                    "duplicate_event_key": duplicate_key,
                }
            )
    pd.DataFrame(labels).to_csv(experiment / "artifacts/manual_labels.csv", index=False, lineterminator="\n")
    pd.DataFrame(event_rows).to_csv(experiment / "artifacts/manual_events.csv", index=False, lineterminator="\n")
    print(f"recorded {len(labels)} human reviews and {len(event_rows)} event rows")


if __name__ == "__main__":
    main()
