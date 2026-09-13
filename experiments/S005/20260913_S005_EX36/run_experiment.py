from __future__ import annotations

import json
import html
import re
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX36"
RELEVANCE = {"CATALYST_RELEVANT", "NOT_CATALYST", "UNRESOLVED"}
DIRECTIONS = {"POSITIVE", "NEGATIVE", "AMBIGUOUS", "NONE"}
EVENT_TYPES = {
    "POLICY_SUPPORT", "REGULATORY_RESTRICTION", "EXPORT_CONTROL", "SUPPLY_DISRUPTION",
    "ORDER_DEMAND", "MASS_PRODUCTION", "PRICE_CHANGE", "CAPEX", "M_AND_A",
    "INDUSTRIAL_INVESTMENT", "EARNINGS_CHANGE", "TECHNOLOGY_BREAKTHROUGH",
    "PRODUCT_RELEASE", "INDUSTRY_DATA", "KEY_PERSONNEL_CHANGE", "CAPITAL_ALLOCATION",
    "LEGAL_RESOLUTION",
    "NONE", "UNRESOLVED",
}


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _evidence_key(value: object) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff.%+\-]+", "", text).lower()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    cache_manifest = _read(artifacts / "cache_manifest.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices") or protocol.get("rule_evaluation"):
        raise ValueError("EX36 may not read returns or evaluate extraction rules")
    cache_path = repo / str(protocol["local_cache"])
    if not cache_path.exists():
        raise FileNotFoundError(f"local news cache missing; run prepare_holdout.py: {cache_path}")
    if raw_file_sha256(cache_path) != cache_manifest["cache_sha256"]:
        raise ValueError("local news cache differs from frozen holdout")

    raw = pd.read_csv(cache_path, keep_default_na=False)
    index = pd.read_csv(artifacts / "holdout_index.csv")
    labels = pd.read_csv(artifacts / "manual_labels.csv", keep_default_na=False)
    events = pd.read_csv(artifacts / "manual_events.csv", keep_default_na=False)
    exclusions = pd.read_csv(artifacts / "holdout_exclusions.csv", keep_default_na=False)
    label_columns = {"sample_id", "relevance", "review_evidence_excerpt", "review_reason"}
    event_columns = {
        "event_id", "sample_id", "entity_scope", "primary_entity", "event_type", "direction",
        "event_summary", "evidence_excerpt", "duplicate_event_key",
    }
    if set(labels.columns) != label_columns or set(events.columns) != event_columns:
        raise ValueError("manual annotation schema differs from frozen schema")
    if set(exclusions.columns) != {"duplicate_event_key", "reason"}:
        raise ValueError("holdout exclusion schema differs from frozen schema")
    if len(index) != 120 or len(labels) != 120 or labels["sample_id"].duplicated().any():
        raise ValueError("EX36 requires exactly 120 unique manual article reviews")
    if set(index["sample_id"]) != set(labels["sample_id"]):
        raise ValueError("manual annotations differ from frozen holdout")
    if not set(labels["relevance"]).issubset(RELEVANCE) or labels["relevance"].eq("").any():
        raise ValueError("invalid or missing relevance label")
    if labels[["review_evidence_excerpt", "review_reason"]].apply(
        lambda column: column.astype(str).str.strip().eq("").any()
    ).any():
        raise ValueError("manual article review has missing audit text")
    source_text = {
        str(row.sample_id): _evidence_key(f"{row.title} {row.content}")
        for row in raw.itertuples(index=False)
    }
    for row in labels.itertuples(index=False):
        if _evidence_key(row.review_evidence_excerpt) not in source_text[str(row.sample_id)]:
            raise ValueError(f"article evidence is not present in cached source: {row.sample_id}")
    if events["event_id"].duplicated().any() or events["event_id"].astype(str).str.strip().eq("").any():
        raise ValueError("manual events require unique non-empty event_id")
    if not set(events["sample_id"]).issubset(set(index["sample_id"])):
        raise ValueError("manual event refers to a sample outside the frozen holdout")
    if not set(events["event_type"]).issubset(EVENT_TYPES - {"NONE", "UNRESOLVED"}):
        raise ValueError("invalid or missing event_type")
    if not set(events["direction"]).issubset(DIRECTIONS - {"NONE"}) or events["direction"].eq("").any():
        raise ValueError("invalid or missing direction")
    required_text = ["entity_scope", "primary_entity", "event_summary", "evidence_excerpt", "duplicate_event_key"]
    if events[required_text].apply(lambda column: column.astype(str).str.strip().eq("").any()).any():
        raise ValueError("manual event has missing audit text")
    for row in events.itertuples(index=False):
        if _evidence_key(row.evidence_excerpt) not in source_text[str(row.sample_id)]:
            raise ValueError(f"event evidence is not present in cached source: {row.event_id}")
    duplicate_semantics = events.groupby("duplicate_event_key", observed=True)[
        ["primary_entity", "event_type"]
    ].nunique()
    if duplicate_semantics.gt(1).any(axis=None):
        raise ValueError("duplicate_event_key combines events with different entity or type")
    if exclusions["duplicate_event_key"].duplicated().any() or exclusions["reason"].str.strip().eq("").any():
        raise ValueError("holdout exclusions must have unique keys and non-empty reasons")
    unique_event_keys = set(events["duplicate_event_key"])
    excluded_event_keys = set(exclusions["duplicate_event_key"])
    if not excluded_event_keys.issubset(unique_event_keys):
        raise ValueError("holdout exclusion refers to an unknown structured event")
    relevant = labels["relevance"].eq("CATALYST_RELEVANT")
    event_sample_ids = set(events["sample_id"])
    if set(labels.loc[relevant, "sample_id"]) != event_sample_ids:
        raise ValueError("each relevant article must have events and only relevant articles may have events")

    reviewed = index[["sample_id", "bucket"]].merge(labels, on="sample_id", validate="one_to_one")
    counts = reviewed.groupby(["bucket", "relevance"], observed=True).size().unstack(fill_value=0)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "annotated_rows": int(len(labels)),
        "structured_events": int(len(events)),
        "unique_structured_events": int(events["duplicate_event_key"].nunique()),
        "excluded_design_events": int(len(excluded_event_keys)),
        "independent_unique_events": int(len(unique_event_keys - excluded_event_keys)),
        "relevant_rows": int(relevant.sum()),
        "unresolved_rows": int(labels["relevance"].eq("UNRESOLVED").sum()),
        "bucket_relevance_counts": {
            str(bucket): {str(label): int(value) for label, value in row.items()}
            for bucket, row in counts.iterrows()
        },
        "cache_sha256": cache_manifest["cache_sha256"],
        "manual_labels_sha256": raw_file_sha256(artifacts / "manual_labels.csv"),
        "manual_events_sha256": raw_file_sha256(artifacts / "manual_events.csv"),
        "holdout_exclusions_sha256": raw_file_sha256(artifacts / "holdout_exclusions.csv"),
        "reads_post_event_prices": False,
        "decision": "MANUAL_EXTRACTION_FEASIBLE_NEW_HOLDOUT_REQUIRED",
    }
    (artifacts / "manual_extraction_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX36 执行\n\n"
        f"状态：`COMPLETE`。人工逐篇完成{len(labels)}篇正文审核，其中可用催化{int(relevant.sum())}篇、"
        f"结构化事件{len(events)}条、去重事件{len(unique_event_keys)}条、独立留出事件"
        f"{len(unique_event_keys - excluded_event_keys)}条、无法判断{int(labels['relevance'].eq('UNRESOLVED').sum())}篇。完整正文保存在本地缓存，"
        "实验档案保存标题、原文哈希、证据摘录、结构化字段和判定理由。未读取任何行情或收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX36 结论\n\n"
        "裁决：`MANUAL_EXTRACTION_FEASIBLE_NEW_HOLDOUT_REQUIRED`。120篇正文中22篇包含可用催化，"
        "形成30条事件、26条去重事件；其中2条事实已在EX35设计样本出现，独立事件剩余24条。"
        "标题词典命中桶只有20/60篇相关，说明关键词仍只能做候选召回；未命中桶也发现2篇相关，"
        "分别是历史成分业绩与半导体材料扩产。\n\n"
        "人工阅读全文并保留原文哈希、证据摘录、判定理由和重复事件键，在小样本上可行。"
        "完整历史库仍应先由确定性程序做高召回候选筛选，再由Codex人工完成语义抽取。"
        "由于后续筛选规则会利用本实验发现，EX36从此属于规则开发样本，不能再作为独立验收集。"
        "必须先冻结候选筛选规则，再从未读原文中抽取新的留出集，验证精确率不低于80%、"
        "召回率不低于60%和复跑一致率100%。在新留出集通过前，禁止全量历史回填和收益检验。\n",
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
            "decision": evidence["decision"],
            "reads_post_event_prices": False,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
