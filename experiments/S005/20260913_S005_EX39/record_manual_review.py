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

EVENT_TITLE_BY_OLD_ID = {
    "EX39-0009": "人均账面价值超550万元 寒武纪124名员工获激励大礼包",
    "EX39-0017": "上海合晶硅材料股份有限公司董事及高级管理人员减持股份计划公告",
    "EX39-0033": "下游需求持续释放 瑞芯微上半年净利同比增逾六成",
    "EX39-0035": "发力AI创新领域 复旦微电上半年净利同比增三倍多",
    "EX39-0037": "PCB产业链半年报亮眼 高端产品供需偏紧或延续至2028年",
    "EX39-0043": "存储与逻辑芯片迭代升级 CMP市场迎来新风口",
    "EX39-0093": "AI算力订单占比升至九成 芯原股份上半年营收近翻倍但亏损持续｜财报解读",
    "EX39-0098": "12天11板大牛股，今起复牌！磷化铟基板价格飙涨，多股业绩向好",
    "EX39-0103": "海光信息高端发力单季首赚超10亿   存货逾75亿预付款倍增保障供应能力",
    "EX39-0105": "富乐德拟募资11.76亿投建七大项目 双主业协同发力半年盈利预增逾五成",
    "EX39-0107": "中际旭创相继入股PCB及散热龙头 H股募资534亿港元加速产业链布局",
    "EX39-0155": "上半年亏损超6亿，芯原股份亟待百亿AI订单扭转局面",
    "EX39-0158": "芯原股份上半年营收接近倍增 充足订单支撑未来业绩增长",
    "EX39-0178": "瑞芯微上半年净利润同比增长近62% 多措并举全力保供",
    "EX39-0193": "狂揽订单151亿，半导体IP龙头芯原股份仍陷亏损，一边股权激励员工一边遭老股东减持",
    "EX39-0197": "芯原股份上半年营收增超9成 预计下半年经调整后EBITDA转正",
    "EX39-0213": "远东股份“AIDC用全合成光纤预制棒制造”项目主体封顶！",
    "EX39-0249": "传音控股上半年净利润增四成，存储涨价备货致经营现金流净流出",
}
EVENTS_BY_TITLE = {EVENT_TITLE_BY_OLD_ID[sample_id]: rows for sample_id, rows in EVENTS.items()}


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
        relevant = row.title in EVENTS_BY_TITLE
        labels.append(
            {
                "sample_id": row.sample_id,
                "relevance": "CATALYST_RELEVANT" if relevant else "NOT_CATALYST",
                "review_evidence_excerpt": row.title,
                "review_reason": "DIRECT_SCOPE_FACT" if relevant else "OUTSIDE_S005_NEWS_SCOPE_OR_NO_NEW_FACT",
            }
        )
        for entity, event_type, direction, summary, duplicate_key in EVENTS_BY_TITLE.get(row.title, []):
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
