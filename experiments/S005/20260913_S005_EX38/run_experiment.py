from __future__ import annotations

import html
import importlib.util
import json
import re
import sys
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX38"
RELEVANCE = {"CATALYST_RELEVANT", "NOT_CATALYST", "UNRESOLVED"}
DIRECTIONS = {"POSITIVE", "NEGATIVE", "AMBIGUOUS"}
EVENT_TYPES = {
    "POLICY_SUPPORT", "REGULATORY_RESTRICTION", "EXPORT_CONTROL", "SUPPLY_DISRUPTION",
    "ORDER_DEMAND", "MASS_PRODUCTION", "PRICE_CHANGE", "CAPEX", "M_AND_A",
    "INDUSTRIAL_INVESTMENT", "EARNINGS_CHANGE", "TECHNOLOGY_BREAKTHROUGH",
    "PRODUCT_RELEASE", "INDUSTRY_DATA", "KEY_PERSONNEL_CHANGE", "CAPITAL_ALLOCATION",
    "LEGAL_RESOLUTION",
}


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _evidence_key(value: object) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff.%+\-]+", "", text).lower()


def _load_prefilter(path: Path):
    spec = importlib.util.spec_from_file_location("s005_ex37_frozen_prefilter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen prefilter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_selected(value: object) -> bool:
    normalized = str(value).strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"invalid sealed selected value: {value}")
    return normalized == "true"


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    cache_manifest = _read(artifacts / "cache_manifest.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices") or protocol.get("event_generation_before_review"):
        raise ValueError("EX38 may not read returns or generate events before manual review")

    ex37 = repo / "experiments/S005/20260913_S005_EX37"
    validate_experiment_archive(ex37)
    frozen_sources = {
        ex37 / "experiment_manifest.json": protocol["frozen_prefilter"]["ex37_manifest_sha256"],
        ex37 / "prefilter.py": protocol["frozen_prefilter"]["code_sha256"],
        ex37 / "artifacts/important_component_entities.csv": protocol["frozen_prefilter"]["entity_snapshot_sha256"],
        artifacts / "aborted_blind_order_index.csv": protocol["holdout"]["aborted_v1_index_sha256"],
    }
    for path, digest in frozen_sources.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"frozen input differs: {path}")

    cache_path = repo / str(protocol["local_cache"])
    sealed_path = repo / str(protocol["sealed_predictions"])
    if raw_file_sha256(cache_path) != cache_manifest["cache_sha256"]:
        raise ValueError("local raw cache differs from frozen holdout")
    if raw_file_sha256(sealed_path) != cache_manifest["sealed_predictions_sha256"]:
        raise ValueError("sealed prediction cache differs from frozen holdout")

    raw = pd.read_csv(cache_path, keep_default_na=False)
    index = pd.read_csv(artifacts / "holdout_index.csv", keep_default_na=False)
    labels = pd.read_csv(artifacts / "manual_labels.csv", keep_default_na=False)
    events = pd.read_csv(artifacts / "manual_events.csv", keep_default_na=False)
    label_columns = {"sample_id", "relevance", "review_evidence_excerpt", "review_reason"}
    event_columns = {
        "event_id", "sample_id", "entity_scope", "primary_entity", "event_type", "direction",
        "event_summary", "evidence_excerpt", "duplicate_event_key",
    }
    if set(labels.columns) != label_columns or set(events.columns) != event_columns:
        raise ValueError("manual annotation schema differs from frozen schema")
    if len(raw) != 115 or len(index) != 115 or len(labels) != 115:
        raise ValueError("EX38 requires exactly 115 frozen manual reviews")
    if labels["sample_id"].duplicated().any() or set(index["sample_id"]) != set(labels["sample_id"]):
        raise ValueError("manual reviews differ from the blind holdout")
    if labels["relevance"].eq("").any() or not set(labels["relevance"]).issubset(RELEVANCE):
        raise ValueError("manual review is incomplete or contains an invalid label")
    if labels[["review_evidence_excerpt", "review_reason"]].apply(
        lambda column: column.astype(str).str.strip().eq("").any()
    ).any():
        raise ValueError("manual review has missing audit text")

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
        raise ValueError("manual event refers to a sample outside the blind holdout")
    if not set(events["event_type"]).issubset(EVENT_TYPES):
        raise ValueError("invalid event type")
    if not set(events["direction"]).issubset(DIRECTIONS):
        raise ValueError("invalid event direction")
    required_text = ["entity_scope", "primary_entity", "event_summary", "evidence_excerpt", "duplicate_event_key"]
    if events[required_text].apply(lambda column: column.astype(str).str.strip().eq("").any()).any():
        raise ValueError("manual event has missing audit text")
    for row in events.itertuples(index=False):
        if _evidence_key(row.evidence_excerpt) not in source_text[str(row.sample_id)]:
            raise ValueError(f"event evidence is not present in cached source: {row.event_id}")
    relevant_ids = set(labels.loc[labels["relevance"].eq("CATALYST_RELEVANT"), "sample_id"])
    if relevant_ids != set(events["sample_id"]):
        raise ValueError("each relevant article must have events and only relevant articles may have events")

    # The manual review is complete and valid above. Only now may the sealed predictions be read.
    sealed = pd.read_csv(sealed_path, keep_default_na=False)
    if len(sealed) != 115 or sealed["sample_id"].duplicated().any():
        raise ValueError("sealed predictions differ from the blind holdout")
    sealed["selected"] = sealed["selected"].map(_parse_selected)
    module = _load_prefilter(ex37 / "prefilter.py")
    entities = pd.read_csv(ex37 / "artifacts/important_component_entities.csv")
    important_names = tuple(sorted(entities["name"].astype(str).unique(), key=lambda value: (-len(value), value)))
    rerun_rows: list[dict[str, object]] = []
    for row in raw.itertuples(index=False):
        result = module.select_news_candidate(row.title, row.content, important_names)
        rerun_rows.append({"sample_id": row.sample_id, "rerun_selected": result.selected, "rerun_route": result.route})
    rerun = pd.DataFrame(rerun_rows)
    comparison = sealed.merge(rerun, on="sample_id", validate="one_to_one")
    comparison["rerun_match"] = (
        comparison["selected"].eq(comparison["rerun_selected"])
        & comparison["route"].eq(comparison["rerun_route"])
    )
    agreement = float(comparison["rerun_match"].mean())

    scored = (
        index.merge(labels, on="sample_id", validate="one_to_one")
        .merge(events.groupby("sample_id", observed=True).size().rename("event_rows"), on="sample_id", how="left")
        .merge(sealed, on="sample_id", validate="one_to_one")
    )
    scored["event_rows"] = scored["event_rows"].fillna(0).astype(int)
    scored["stratum"] = scored["selected"].map({True: "PREFILTER_SELECTED", False: "PREFILTER_NOT_SELECTED"})
    scored.to_csv(artifacts / "scored_holdout.csv", index=False, lineterminator="\n")

    selected_pool = int(cache_manifest["pool_counts"]["PREFILTER_SELECTED"])
    not_selected_pool = int(cache_manifest["pool_counts"]["PREFILTER_NOT_SELECTED"])
    selected_sample = scored.loc[scored["selected"]]
    not_selected_sample = scored.loc[~scored["selected"]]
    relevant_selected = int(selected_sample["relevance"].eq("CATALYST_RELEVANT").sum())
    relevant_not_selected = int(not_selected_sample["relevance"].eq("CATALYST_RELEVANT").sum())
    precision = relevant_selected / len(selected_sample) if len(selected_sample) else 0.0
    not_selected_prevalence = relevant_not_selected / len(not_selected_sample) if len(not_selected_sample) else 0.0
    estimated_selected_relevant = selected_pool * precision
    estimated_not_selected_relevant = not_selected_pool * not_selected_prevalence
    estimated_total_relevant = estimated_selected_relevant + estimated_not_selected_relevant
    recall = estimated_selected_relevant / estimated_total_relevant if estimated_total_relevant else 0.0
    thresholds = protocol["acceptance"]
    accepted = (
        precision >= float(thresholds["minimum_precision"])
        and recall >= float(thresholds["minimum_recall"])
        and agreement >= float(thresholds["deterministic_rerun_agreement"])
    )
    decision = "PREFILTER_ACCEPTED_FOR_FULL_MANUAL_BACKFILL" if accepted else "PREFILTER_REJECTED_NO_BACKFILL"
    counts = scored.groupby(["stratum", "relevance"], observed=True).size().unstack(fill_value=0)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "manual_review_rows": int(len(labels)),
        "relevant_rows": int(labels["relevance"].eq("CATALYST_RELEVANT").sum()),
        "unresolved_rows": int(labels["relevance"].eq("UNRESOLVED").sum()),
        "structured_event_rows": int(len(events)),
        "unique_structured_events": int(events["duplicate_event_key"].nunique()),
        "pool_counts": cache_manifest["pool_counts"],
        "sample_counts": {
            "PREFILTER_SELECTED": int(len(selected_sample)),
            "PREFILTER_NOT_SELECTED": int(len(not_selected_sample)),
        },
        "sample_relevant_counts": {
            "PREFILTER_SELECTED": relevant_selected,
            "PREFILTER_NOT_SELECTED": relevant_not_selected,
        },
        "false_positive_rows": int(len(selected_sample) - relevant_selected),
        "false_negative_sample_rows": relevant_not_selected,
        "stratum_relevance_counts": {
            str(stratum): {str(label): int(value) for label, value in row.items()}
            for stratum, row in counts.iterrows()
        },
        "precision": precision,
        "estimated_recall": recall,
        "not_selected_sample_prevalence": not_selected_prevalence,
        "estimated_relevant_rows": {
            "PREFILTER_SELECTED": estimated_selected_relevant,
            "PREFILTER_NOT_SELECTED": estimated_not_selected_relevant,
        },
        "candidate_reduction": 1.0 - selected_pool / (selected_pool + not_selected_pool),
        "deterministic_rerun_agreement": agreement,
        "acceptance": thresholds,
        "accepted": accepted,
        "decision": decision,
        "cache_sha256": cache_manifest["cache_sha256"],
        "sealed_predictions_sha256": cache_manifest["sealed_predictions_sha256"],
        "manual_labels_sha256": raw_file_sha256(artifacts / "manual_labels.csv"),
        "manual_events_sha256": raw_file_sha256(artifacts / "manual_events.csv"),
        "reads_post_event_prices": False,
    }
    (artifacts / "evaluation_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX38 执行\n\n"
        f"状态：`COMPLETE`。盲法人工审核{len(labels)}篇，相关{evidence['relevant_rows']}篇、"
        f"结构化事件{len(events)}条、去重事实{evidence['unique_structured_events']}条。"
        f"解封后，命中层精确率{precision:.2%}，分层加权召回率{recall:.2%}，"
        f"确定性复跑一致率{agreement:.2%}，候选阅读量缩减{evidence['candidate_reduction']:.2%}。"
        "全程未读取事件后行情或收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "预筛规则通过独立留出验收，可作为全量历史新闻的候选召回器；"
        "后续仍必须由Codex阅读全文并人工抽取结构化事实，程序不得直接生成事件或方向。"
        if accepted
        else
        "预筛规则未通过独立留出验收，禁止用于全量历史回填。该冻结版本不得用留出答案修补；"
        "如继续新闻路线，必须登记新的规则版本并使用另一批未读原文重新验收。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX38 结论\n\n"
        f"裁决：`{decision}`。{conclusion}\n\n"
        f"命中层35篇中有25篇相关、10篇误报；未命中抽样80篇中发现2篇漏报。当前规则将完整候选池压缩了"
        f"{evidence['candidate_reduction']:.2%}，压缩幅度过大，漏报在2476篇未命中总体中被分层加权后，"
        "召回率点估计只有28.77%。主要失败类型是直接半导体公司的业绩事实和碳化硅订单事实未被动作词覆盖，"
        "同时IPO、行情复述、业绩说明会和跨行业芯片报道仍产生误报。\n\n"
        "本实验只验证人工阅读成本能否被可靠压缩。精确率、召回率和结构化事件数量均不构成Alpha证据；"
        "在后续机制预注册完成前，仍禁止读取事件后收益。\n",
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
