from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX37"


def _load_prefilter(path: Path):
    spec = importlib.util.spec_from_file_location("s005_ex37_prefilter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen prefilter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices") or protocol.get("event_generation"):
        raise ValueError("EX37 may not read returns or generate events")

    ex35 = repo / "experiments/S005/20260913_S005_EX35"
    ex36 = repo / "experiments/S005/20260913_S005_EX36"
    validate_experiment_archive(ex35)
    validate_experiment_archive(ex36)
    entities_path = artifacts / "important_component_entities.csv"
    if not entities_path.exists():
        raise FileNotFoundError("run build_inputs.py before EX37")
    entities = pd.read_csv(entities_path)
    important_names = tuple(sorted(entities["name"].dropna().astype(str).unique(), key=lambda value: (-len(value), value)))
    if not important_names:
        raise ValueError("important component entity snapshot is empty")

    raw36_path = repo / ".tmp/research_cache/S005/news/ex36_sina_sample.csv.gz"
    if not raw36_path.exists():
        raise FileNotFoundError("EX36 local raw cache is required for rule development")
    raw36 = pd.read_csv(raw36_path, keep_default_na=False)
    index36 = pd.read_csv(ex36 / "artifacts/holdout_index.csv", keep_default_na=False)
    labels36 = pd.read_csv(ex36 / "artifacts/manual_labels.csv", keep_default_na=False)
    dev36 = (
        index36[["sample_id", "title"]]
        .merge(raw36[["sample_id", "content"]], on="sample_id", validate="one_to_one")
        .merge(labels36[["sample_id", "relevance"]], on="sample_id", validate="one_to_one")
    )
    dev36.insert(0, "source_experiment", "EX36")

    sample35 = pd.read_csv(ex35 / "artifacts/audited_sample.csv")
    raw34 = pd.read_csv(repo / "experiments/S005/20260913_S005_EX34/artifacts/semantic_audit_sample.csv.gz")
    raw35 = raw34.loc[raw34["requested_source"].eq("新浪财经"), ["title", "content"]].drop_duplicates("title")
    dev35 = sample35[["title", "label"]].merge(raw35, on="title", validate="one_to_one")
    dev35 = dev35.rename(columns={"label": "relevance"})
    dev35.insert(0, "sample_id", [f"EX35-{value:03d}" for value in range(1, len(dev35) + 1)])
    dev35.insert(0, "source_experiment", "EX35")
    development = pd.concat([dev35, dev36], ignore_index=True)

    module = _load_prefilter(experiment / "prefilter.py")
    rows: list[dict[str, object]] = []
    for row in development.itertuples(index=False):
        result = module.select_news_candidate(row.title, row.content, important_names)
        rows.append(
            {
                "source_experiment": row.source_experiment,
                "sample_id": row.sample_id,
                "title": row.title,
                "manual_relevance": row.relevance,
                "selected": result.selected,
                "route": result.route,
                "matched_entity": result.matched_entity,
                "matched_domain": result.matched_domain,
                "matched_action": result.matched_action,
                "noise_marker": result.noise_marker,
            }
        )
    scored = pd.DataFrame(rows)
    scored.to_csv(artifacts / "development_predictions.csv", index=False, lineterminator="\n")
    truth = scored["manual_relevance"].eq("CATALYST_RELEVANT")
    selected = scored["selected"].astype(bool)
    tp = int((truth & selected).sum())
    fp = int((~truth & selected).sum())
    fn = int((truth & ~selected).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "sample_role": "RULE_DEVELOPMENT_ONLY",
        "development_rows": int(len(scored)),
        "development_relevant_rows": int(truth.sum()),
        "selected_rows": int(selected.sum()),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "development_precision": precision,
        "development_recall": recall,
        "prefilter_sha256": raw_file_sha256(experiment / "prefilter.py"),
        "entity_snapshot_sha256": raw_file_sha256(entities_path),
        "reads_post_event_prices": False,
        "decision": "PREFILTER_FROZEN_FOR_NEW_HOLDOUT",
    }
    (artifacts / "development_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX37 执行\n\n"
        f"状态：`COMPLETE`。规则开发样本{len(scored)}篇，其中人工相关{int(truth.sum())}篇；"
        f"预筛选中{int(selected.sum())}篇，开发样本精确率{precision:.2%}、召回率{recall:.2%}。"
        "这些数字只用于诊断和冻结规则，不能作为独立验收结果。全程未读取行情或收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX37 结论\n\n"
        "裁决：`PREFILTER_FROZEN_FOR_NEW_HOLDOUT`。程序只输出需要Codex阅读全文的候选，"
        "不得直接生成结构化事件或方向。EX35与EX36均为规则开发样本，开发指标不代表泛化能力。\n\n"
        "`prefilter.py`、重要成分实体快照和两者哈希已经冻结。下一步只能在全新未读留出集上"
        "原样验收精确率、召回率和复跑一致性；不得根据留出答案修改本版本，也不得在验收前读取收益。\n",
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
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
