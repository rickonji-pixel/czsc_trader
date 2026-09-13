from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX39"
RELEVANCE = {"CATALYST_RELEVANT", "NOT_CATALYST", "UNRESOLVED"}


def _key(value: object) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff.%+\-]+", "", text).lower()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    cache_manifest = json.loads((artifacts / "cache_manifest.json").read_text(encoding="utf-8"))
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from protocol")
    if protocol.get("semantic_program_filter") or protocol.get("reads_post_event_prices"):
        raise ValueError("EX39 may not use a semantic program filter or read returns")
    cache_path = repo / protocol["local_cache"]
    if raw_file_sha256(cache_path) != cache_manifest["cache_sha256"]:
        raise ValueError("raw cache differs from frozen acquisition manifest")

    raw = pd.read_csv(cache_path, keep_default_na=False)
    index = pd.read_csv(artifacts / "article_index.csv", keep_default_na=False)
    labels = pd.read_csv(artifacts / "manual_labels.csv", keep_default_na=False)
    events = pd.read_csv(artifacts / "manual_events.csv", keep_default_na=False)
    if len(index) != 250 or len(labels) != 250 or labels["sample_id"].duplicated().any():
        raise ValueError("manual pilot must contain exactly 250 unique reviews")
    if set(index["sample_id"]) != set(labels["sample_id"]):
        raise ValueError("manual review differs from fixed pilot")
    if labels["relevance"].eq("").any() or not set(labels["relevance"]).issubset(RELEVANCE):
        raise ValueError("manual review is incomplete")
    if labels[["review_evidence_excerpt", "review_reason"]].apply(
        lambda column: column.astype(str).str.strip().eq("").any()
    ).any():
        raise ValueError("manual review has missing audit text")
    source_text = {
        str(row.sample_id): _key(f"{row.title} {row.content}")
        for row in raw.loc[raw["sample_id"].isin(index["sample_id"])].itertuples(index=False)
    }
    for row in labels.itertuples(index=False):
        if _key(row.review_evidence_excerpt) not in source_text[row.sample_id]:
            raise ValueError(f"label evidence not found in source: {row.sample_id}")
    required_event_text = [
        "event_id", "sample_id", "published_at", "primary_entity", "event_type", "direction",
        "event_summary", "evidence_excerpt", "duplicate_event_key",
    ]
    if set(events.columns) != set(required_event_text):
        raise ValueError("event schema differs from protocol")
    if events[required_event_text].apply(lambda column: column.astype(str).str.strip().eq("").any()).any():
        raise ValueError("manual event has missing fields")
    if events["event_id"].duplicated().any() or not set(events["sample_id"]).issubset(set(index["sample_id"])):
        raise ValueError("manual event identity is invalid")
    for row in events.itertuples(index=False):
        if _key(row.evidence_excerpt) not in source_text[row.sample_id]:
            raise ValueError(f"event evidence not found in source: {row.event_id}")
    relevant_ids = set(labels.loc[labels["relevance"].eq("CATALYST_RELEVANT"), "sample_id"])
    event_ids = set(events["sample_id"])
    review_coverage = len(labels) / len(index)
    relevant_event_coverage = len(relevant_ids & event_ids) / len(relevant_ids) if relevant_ids else 0.0
    unresolved_ratio = float(labels["relevance"].eq("UNRESOLVED").mean())
    unique_events = int(events["duplicate_event_key"].nunique())
    gates = protocol["acceptance"]
    passed = (
        review_coverage == float(gates["review_coverage"])
        and relevant_event_coverage == float(gates["relevant_event_coverage"])
        and not (event_ids - relevant_ids)
        and unresolved_ratio <= float(gates["maximum_unresolved_ratio"])
        and unique_events >= int(gates["minimum_unique_events"])
    )
    decision = "MANUAL_NEWS_PIPELINE_FEASIBLE" if passed else "MANUAL_NEWS_PIPELINE_NOT_FEASIBLE"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "cached_article_rows": int(len(raw)),
        "manual_pilot_rows": int(len(index)),
        "reviewed_rows": int(len(labels)),
        "relevant_article_rows": int(len(relevant_ids)),
        "event_rows": int(len(events)),
        "unique_events": unique_events,
        "review_coverage": review_coverage,
        "relevant_event_coverage": relevant_event_coverage,
        "unresolved_ratio": unresolved_ratio,
        "prevalence_inference_allowed": False,
        "semantic_program_filter": False,
        "reads_post_event_prices": False,
        "decision": decision,
    }
    (artifacts / "evaluation_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX39 执行\n\n"
        f"状态：`COMPLETE`。完整缓存{len(raw)}篇原文，从中固定取时间最早的{len(index)}篇作为人工试点。"
        f"Codex逐篇审核后识别{len(relevant_ids)}篇相关报道，形成{len(events)}条事件记录、"
        f"{unique_events}个去重事实；审核覆盖率{review_coverage:.2%}，相关篇事件覆盖率"
        f"{relevant_event_coverage:.2%}，无法判断比例{unresolved_ratio:.2%}。未使用程序语义筛选，"
        "未读取事件后的行情或收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX39 结论\n\n"
        f"裁决：`{decision}`。人工筛选和结构化抽取链路可以端到端运行，原文、文章级判断、证据、"
        "发布时间和重复事实键均可审计复用。250篇试点由抓取规模可见后收敛，不能据此估计全天或全历史"
        "事件发生率，也不能证明事件具有Alpha。\n\n"
        "下一步先由Codex继续分批生产人工事件数据，再单独预注册事件到可交易信号的映射和竞争解释。"
        "只有人工流程形成足够样本并确认有研究价值后，才评审程序化实现；程序优先自动化获取、去重、"
        "任务编排和校验，不替代Codex的语义判断。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "status": "COMPLETE",
            "decision": decision,
            "reads_post_event_prices": False,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
