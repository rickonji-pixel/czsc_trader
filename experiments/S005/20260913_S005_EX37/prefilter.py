from __future__ import annotations

import re
from dataclasses import dataclass


def _has(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text.lower() for term in terms)


STRONG_FACT_ACTIONS = (
    "业绩", "营收", "净利润", "净利", "毛利率", "扭亏", "预增", "预亏", "亏损扩大",
    "订单", "中标", "签订", "签约", "在手", "需求强劲", "供不应求", "短缺",
    "涨价", "提价", "调价", "降价",
    "投资", "投建", "扩产", "产能", "投产", "满产", "募资", "定增", "延期",
    "收购", "并购", "重组", "股权", "增资", "融资", "举牌", "加仓", "增配",
    "量产", "流片", "技术突破", "产品发布", "获批",
    "出口管制", "制裁", "禁令", "补贴", "关税", "监管新规",
    "核心技术人员离职", "产量", "销量",
)

SEMI_DOMAIN = (
    "半导体", "芯片", "集成电路", "晶圆", "光刻", "存储", "封装", "封测",
    "掩模", "EDA", "GPU", "SoC", "MCU", "先进制程", "硅片", "刻蚀",
)

AI_CORE_DOMAIN = (
    "AI芯片", "算力芯片", "AI服务器", "数据中心", "AIDC", "算力中心", "算力",
    "高性能计算", "HBM", "高速互联",
)

AI_DIRECT_ACTIONS = (
    "订单", "中标", "签订", "签约", "在手", "需求", "供不应求", "短缺",
    "扩产", "产能", "投产", "满产", "资本开支", "采购", "覆盖头部客户", "配套供应",
)

STAR_POLICY_DOMAIN = ("科创板", "科创50", "硬科技")
POLICY_ACTIONS = (
    "发布新规", "出台", "实施", "改革", "扩容", "调整规则", "降低门槛",
    "提高门槛", "暂停", "恢复", "纳入", "移出", "补贴", "税收优惠",
)

ABSOLUTE_NOISE_MARKERS = (
    "证券-", "证券：", "证券:", "机构大幅上调", "业绩预测", "研报",
    "峰会", "研讨会", "圆满举行", "公告与交易提示", "股海导航", "晚报|", "午报",
    "首次公开发行", "上市公告书", "申购情况", "中签率", "冲刺“", "拟A+H",
)

MARKET_NOISE_MARKERS = (
    "收评", "午评", "午盘", "收盘", "盘中", "行情", "涨停", "跌停", "领涨",
    "飙涨", "大涨", "走强", "爆发", "异动", "历史新高", "ETF午评", "ETF收评",
    "机构：", "看好", "建议关注",
)

PURE_MARKET_ACTIONS = ("涨停", "跌停", "上涨", "下跌", "收涨", "收跌", "高开", "低开", "反弹")
HARD_FACT_OVERRIDE = (
    "订单", "中标", "净利润", "营收", "涨价", "提价", "短缺", "定增", "募资",
    "投建", "扩产", "投产", "满产", "收购", "并购", "增资", "核心技术人员离职",
    "出口管制", "制裁", "产量",
)


@dataclass(frozen=True)
class PrefilterResult:
    selected: bool
    route: str
    matched_entity: str
    matched_domain: str
    matched_action: str
    noise_marker: str


def _first(text: str, terms: tuple[str, ...]) -> str:
    return next((term for term in terms if term.lower() in text.lower()), "")


def select_news_candidate(title: object, content: object, important_entities: tuple[str, ...]) -> PrefilterResult:
    title_text = re.sub(r"\s+", " ", str(title)).strip()
    lead = re.sub(r"\s+", " ", str(content)).strip()[:500]
    searchable = f"{title_text} {lead}"

    entity = _first(title_text, important_entities)
    semi = _first(title_text, SEMI_DOMAIN)
    ai_core = _first(title_text, AI_CORE_DOMAIN)
    star = _first(title_text, STAR_POLICY_DOMAIN)
    action = _first(title_text, STRONG_FACT_ACTIONS)
    absolute_noise = _first(title_text, ABSOLUTE_NOISE_MARKERS)
    market_noise = _first(title_text, MARKET_NOISE_MARKERS)
    noise = absolute_noise or market_noise
    hard_override = _first(title_text, HARD_FACT_OVERRIDE)

    if absolute_noise or "半导体显示" in title_text:
        return PrefilterResult(False, "ABSOLUTE_NOISE", entity, semi, action, noise or "半导体显示")

    # Directly named important components still require a concrete new fact.
    if entity and action:
        return PrefilterResult(True, "IMPORTANT_COMPONENT_ACTION", entity, "", action, noise)

    # Direct semiconductor-chain facts are allowed through market-style headlines only
    # when a hard factual action is also visible in the title.
    if semi and action and (not noise or hard_override):
        return PrefilterResult(True, "SEMICONDUCTOR_ACTION", "", semi, action, noise)

    if ai_core and _has(title_text, AI_DIRECT_ACTIONS) and (not noise or hard_override):
        return PrefilterResult(
            True,
            "AI_CORE_DIRECT_ACTION",
            "",
            ai_core,
            _first(title_text, AI_DIRECT_ACTIONS),
            noise,
        )

    if star and _has(title_text, POLICY_ACTIONS) and not noise:
        return PrefilterResult(
            True,
            "STAR_POLICY_ACTION",
            "",
            star,
            _first(title_text, POLICY_ACTIONS),
            noise,
        )

    # Two narrow recall repairs discovered in EX36: an important component can be
    # absent from the title but named with a concrete action in the opening paragraph;
    # semiconductor material capacity can be stated without a generic sector word.
    lead_entity = _first(lead, important_entities)
    lead_action = _first(searchable, STRONG_FACT_ACTIONS)
    if lead_entity and lead_action and not noise:
        return PrefilterResult(True, "IMPORTANT_COMPONENT_LEAD_ACTION", lead_entity, "", lead_action, noise)
    if _has(searchable, ("半导体材料", "掩模基板", "离型膜")) and _has(searchable, ("投建", "扩产", "产能", "追加投资")) and not noise:
        return PrefilterResult(
            True,
            "SEMICONDUCTOR_MATERIAL_CAPACITY",
            "",
            _first(searchable, ("半导体材料", "掩模基板", "离型膜")),
            _first(searchable, ("投建", "扩产", "产能", "追加投资")),
            noise,
        )

    return PrefilterResult(False, "NO_MATCH", entity or lead_entity, semi or ai_core or star, action or lead_action, noise)
