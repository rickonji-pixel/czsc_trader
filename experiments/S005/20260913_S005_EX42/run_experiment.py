from __future__ import annotations

import json
from pathlib import Path
import re

import pandas as pd

from czsc_trader.application.errors import ExecutionError
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from czsc_trader.news_events.service import NewsExtractionCommand, run_news_extraction


EXPERIMENT_ID = "20260913_S005_EX42"


def _normalized(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).lower()


def _verify(path: Path, digest: str) -> None:
    if raw_file_sha256(path) != digest:
        raise ValueError(f"preregistered source differs: {path}")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))

    source_path = repo / protocol["source"]["path"]
    labels_path = repo / protocol["gold"]["labels_path"]
    events_path = repo / protocol["gold"]["events_path"]
    scope_path = repo / protocol["scope"]["path"]
    _verify(source_path, protocol["source"]["sha256"])
    _verify(labels_path, protocol["gold"]["labels_sha256"])
    _verify(events_path, protocol["gold"]["events_sha256"])
    _verify(scope_path, protocol["scope"]["sha256"])

    all_articles = pd.read_csv(source_path, keep_default_na=False)
    start = int(protocol["source"]["start_offset"])
    count = int(protocol["source"]["article_count"])
    holdout = all_articles.iloc[start : start + count].copy()
    if len(holdout) != count:
        raise ValueError("independent holdout is incomplete")
    runtime_dir = repo / ".tmp/news_events/S005-ex42-holdout"
    subset_path = runtime_dir / "holdout_articles.csv.gz"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    holdout.to_csv(subset_path, index=False, compression="gzip", lineterminator="\n")

    try:
        run_news_extraction(
            NewsExtractionCommand(
                input_path=subset_path,
                scope_path=scope_path,
                output_dir=runtime_dir / "extraction",
                workers=int(protocol["workers"]),
                env_file=repo / ".env",
            )
        )
    except ExecutionError as exc:
        if exc.code != "news_extraction_incomplete":
            raise
    extraction_manifest = json.loads(
        (runtime_dir / "extraction/manifest.json").read_text(encoding="utf-8")
    )

    # Gold labels are deliberately loaded only after every model result has been persisted.
    gold = pd.read_csv(labels_path, keep_default_na=False).set_index("sample_id")
    gold = gold.loc[holdout["sample_id"]].copy()
    predicted = pd.read_json(runtime_dir / "extraction/reviews.jsonl", lines=True).set_index("sample_id")
    predicted = predicted.reindex(holdout["sample_id"]).fillna(
        {
            "relevance": "UNRESOLVED",
            "review_reason": "MODEL_OUTPUT_REJECTED",
            "relevance_evidence": "",
        }
    )
    joined = gold.join(predicted[["relevance", "review_reason", "relevance_evidence"]], rsuffix="_predicted")
    joined = joined.rename(columns={"relevance": "gold_relevance", "relevance_predicted": "predicted_relevance"})
    expected = joined["gold_relevance"].eq("CATALYST_RELEVANT")
    observed = joined["predicted_relevance"].eq("CATALYST_RELEVANT")
    tp = int((expected & observed).sum())
    fp = int((~expected & observed).sum())
    fn = int((expected & ~observed).sum())
    tn = int((~expected & ~observed).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0

    model_events = pd.read_json(runtime_dir / "extraction/events.jsonl", lines=True)
    manual_events = pd.read_csv(events_path, keep_default_na=False)
    manual_events = manual_events.loc[manual_events["sample_id"].isin(holdout["sample_id"])].copy()
    comparisons: list[dict[str, object]] = []
    for row in model_events.itertuples(index=False):
        choices = manual_events.loc[manual_events["sample_id"].eq(row.sample_id)]
        strict = any(
            _normalized(item.primary_entity) == _normalized(row.primary_entity)
            and item.event_type == row.event_type
            and item.direction == row.direction
            for item in choices.itertuples(index=False)
        )
        comparisons.append(
            {
                "sample_id": row.sample_id,
                "model_entity": row.primary_entity,
                "model_event_type": row.event_type,
                "model_direction": row.direction,
                "strict_match_any_manual_event": strict,
                "manual_event_count": int(len(choices)),
            }
        )
    comparison = pd.DataFrame(comparisons)
    strict_matches = int(comparison["strict_match_any_manual_event"].sum()) if not comparison.empty else 0
    completion_rate = float(extraction_manifest["completed_count"] / count)
    checks = {
        "completion_rate": completion_rate >= float(protocol["acceptance"]["completion_rate_min"]),
        "article_precision": precision >= float(protocol["acceptance"]["article_precision_min"]),
        "article_recall": recall >= float(protocol["acceptance"]["article_recall_min"]),
    }
    decision = "MAAS_EXTRACTION_VALIDATED_FOR_BACKFILL" if all(checks.values()) else "MAAS_EXTRACTION_REJECTED"
    metrics = {
        "article_count": count,
        "gold_relevant": int(expected.sum()),
        "predicted_relevant": int(observed.sum()),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "precision": precision,
        "recall": recall,
        "completion_rate": completion_rate,
        "canonical_primary_events": int(len(model_events)),
        "primary_event_strict_matches": strict_matches,
        "primary_event_strict_match_rate_among_predictions": (
            strict_matches / len(model_events) if len(model_events) else 0.0
        ),
        "additional_events": int(extraction_manifest["additional_event_count"]),
        "reused_articles": int(extraction_manifest["reused_count"]),
        "failed_articles": int(extraction_manifest["failure_count"]),
    }
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "checks": checks,
        "metrics": metrics,
        "model": extraction_manifest["model"],
        "prompt_and_scope_were_frozen_before_gold_release": True,
        "reads_prices_or_returns": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "runtime_audit_dir": str(runtime_dir / "extraction"),
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    joined.reset_index().to_csv(artifacts / "classification_comparison.csv", index=False, lineterminator="\n")
    comparison.to_csv(artifacts / "primary_event_comparison.csv", index=False, lineterminator="\n")
    (artifacts / "validation_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX42 执行\n\n"
        f"状态：`COMPLETE`。独立盲测{count}篇，完成率{completion_rate:.2%}；文章相关性精确率"
        f"{precision:.2%}、召回率{recall:.2%}。模型输出{len(model_events)}条核心事件和"
        f"{extraction_manifest['additional_event_count']}条同稿附加事件；核心事件严格匹配人工事件"
        f"{strict_matches}/{len(model_events)}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX42 结论\n\n"
        f"裁决：`{decision}`。本轮只验证新闻数据生产链路，没有读取行情或收益，不产生交易信号、"
        "不创建候选，也不修改SM或PTE。核心事件严格一致率是诊断结果，不追加事后通过线。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": protocol["development_cutoff"],
            "status": "COMPLETE",
            "decision": decision,
            "reads_prices_or_returns": False,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
