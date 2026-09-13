from __future__ import annotations

from pathlib import Path

import pandas as pd


EXPERIMENT_ID = "20260913_S005_EX38"


# These judgements were made from the blinded title/body queue.  This file deliberately
# contains no prefilter output and must be completed before run_experiment.py opens the
# sealed prediction cache.
EVENTS: dict[str, tuple[str, str, str, str, str]] = {
    "EX38-003": ("海光信息", "EARNINGS_CHANGE", "POSITIVE", "海光信息披露2025年三季度收入和利润增长", "海光信息|EARNINGS_CHANGE|2025Q3"),
    "EX38-008": ("中科飞测", "EARNINGS_CHANGE", "NEGATIVE", "中科飞测披露2024年度首次亏损", "中科飞测|EARNINGS_CHANGE|2024FY"),
    "EX38-009": ("精测电子", "M_AND_A", "AMBIGUOUS", "精测电子筹划购买半导体子公司股权", "精测电子|M_AND_A|20260715"),
    "EX38-010": ("晶盛机电", "ORDER_DEMAND", "POSITIVE", "晶盛机电披露碳化硅产品送样和批量订单进展", "晶盛机电|ORDER_DEMAND|20260414"),
    "EX38-020": ("佰维存储", "EARNINGS_CHANGE", "POSITIVE", "佰维存储披露2026年上半年业绩预增", "佰维存储|EARNINGS_CHANGE|2026H1"),
    "EX38-022": ("帝科股份", "M_AND_A", "AMBIGUOUS", "帝科股份拟现金收购江苏晶凯", "帝科股份|M_AND_A|20251015"),
    "EX38-027": ("横店东磁", "MASS_PRODUCTION", "POSITIVE", "横店东磁披露芯片电感规模化生产进展", "横店东磁|MASS_PRODUCTION|2025FY"),
    "EX38-032": ("存储芯片", "PRICE_CHANGE", "POSITIVE", "存储价格上涨并伴随相关公司业绩增长", "存储芯片|PRICE_CHANGE|20260715"),
    "EX38-044": ("寒武纪", "CAPITAL_ALLOCATION", "AMBIGUOUS", "寒武纪与地方国资设立股权投资基金", "寒武纪|CAPITAL_ALLOCATION|20250415"),
    "EX38-045": ("仕佳光子", "CAPEX", "POSITIVE", "仕佳光子定增投入连续波激光器芯片项目", "仕佳光子|CAPEX|20260715"),
    "EX38-046": ("英唐智控", "EARNINGS_CHANGE", "NEGATIVE", "英唐智控披露业绩下降及芯片业务进展受阻", "英唐智控|EARNINGS_CHANGE|2025H1"),
    "EX38-048": ("金山办公", "EARNINGS_CHANGE", "POSITIVE", "金山办公披露2026年一季度业绩预增", "金山办公|EARNINGS_CHANGE|2026Q1"),
    "EX38-051": ("宏微科技", "EARNINGS_CHANGE", "NEGATIVE", "宏微科技披露2024年度亏损", "宏微科技|EARNINGS_CHANGE|2024FY"),
    "EX38-055": ("晶晨股份", "M_AND_A", "AMBIGUOUS", "晶晨股份披露收购芯迈微进展", "晶晨股份|M_AND_A|20250915"),
    "EX38-057": ("海光信息", "EARNINGS_CHANGE", "POSITIVE", "海光信息披露2025年三季度收入和利润增长", "海光信息|EARNINGS_CHANGE|2025Q3"),
    "EX38-062": ("电科芯片", "CAPITAL_ALLOCATION", "POSITIVE", "电科芯片控股股东一致行动人完成增持", "电科芯片|CAPITAL_ALLOCATION|20250415"),
    "EX38-067": ("海光信息", "EARNINGS_CHANGE", "POSITIVE", "海光信息披露2025年三季度利润增长", "海光信息|EARNINGS_CHANGE|2025Q3"),
    "EX38-069": ("佰维存储", "EARNINGS_CHANGE", "POSITIVE", "佰维存储披露2026年一季度收入和利润增长", "佰维存储|EARNINGS_CHANGE|2026Q1"),
    "EX38-072": ("仕佳光子", "CAPEX", "POSITIVE", "仕佳光子定增扩充高速光芯片产能", "仕佳光子|CAPEX|20260715"),
    "EX38-079": ("概伦电子", "EARNINGS_CHANGE", "POSITIVE", "概伦电子披露收入和利润增长", "概伦电子|EARNINGS_CHANGE|2025FY"),
    "EX38-080": ("芯原股份", "M_AND_A", "AMBIGUOUS", "芯原股份拟收购逐点半导体控制权", "芯原股份|M_AND_A|20251015"),
    "EX38-092": ("沪电股份", "ORDER_DEMAND", "POSITIVE", "沪电股份披露AI服务器相关需求和产能进展", "沪电股份|ORDER_DEMAND|2026H1"),
    "EX38-100": ("佰维存储", "EARNINGS_CHANGE", "POSITIVE", "佰维存储披露2026年上半年业绩预增", "佰维存储|EARNINGS_CHANGE|2026H1"),
    "EX38-103": ("容百科技", "CAPEX", "AMBIGUOUS", "容百科技拟扩大磷酸铁锂产能", "容百科技|CAPEX|20260415"),
    "EX38-104": ("寒武纪", "EARNINGS_CHANGE", "POSITIVE", "寒武纪披露2021年度收入增长", "寒武纪|EARNINGS_CHANGE|2021FY"),
    "EX38-111": ("耐科装备", "EARNINGS_CHANGE", "POSITIVE", "耐科装备披露2024年度收入增长", "耐科装备|EARNINGS_CHANGE|2024FY"),
    "EX38-115": ("容百科技", "CAPEX", "AMBIGUOUS", "容百科技拟扩大磷酸铁锂产能", "容百科技|CAPEX|20260415"),
}


def main() -> None:
    experiment = Path(__file__).resolve().parent
    index = pd.read_csv(experiment / "artifacts/holdout_index.csv", keep_default_na=False)
    if len(index) != 115 or index["sample_id"].duplicated().any():
        raise ValueError("EX38 manual review expects the frozen 115-row blind queue")
    if not set(EVENTS).issubset(set(index["sample_id"])):
        raise ValueError("manual event judgement refers to an unknown blind sample")

    labels: list[dict[str, str]] = []
    event_rows: list[dict[str, str]] = []
    for row in index.itertuples(index=False):
        relevant = row.sample_id in EVENTS
        labels.append(
            {
                "sample_id": row.sample_id,
                "relevance": "CATALYST_RELEVANT" if relevant else "NOT_CATALYST",
                "review_evidence_excerpt": row.title,
                "review_reason": "DIRECT_FROZEN_SCOPE_FACT" if relevant else "OUTSIDE_FROZEN_EVENT_SCOPE",
            }
        )
        if relevant:
            entity, event_type, direction, summary, duplicate_key = EVENTS[row.sample_id]
            event_rows.append(
                {
                    "event_id": f"EVT-{len(event_rows) + 1:03d}",
                    "sample_id": row.sample_id,
                    "entity_scope": "IMPORTANT_COMPONENT_OR_DIRECT_INDUSTRY_CHAIN",
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
    print(f"completed blind manual review: {len(labels)} articles, {len(event_rows)} event rows")


if __name__ == "__main__":
    main()
