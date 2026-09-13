from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX35"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/semantic_audit_sample.csv.gz": source_spec["sample_sha256"],
        source / "artifacts/feasibility_evidence.json": source_spec["evidence_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if protocol.get("reads_post_event_prices") or protocol.get("direction_classification"):
        raise ValueError("EX35 may only audit semantic relevance")

    source_protocol = _read(source / "artifacts/protocol.json")
    data = pd.read_csv(source / "artifacts/semantic_audit_sample.csv.gz")
    data = data.loc[data["requested_source"].eq(protocol["audit_source"])].copy()
    terms = [str(term) for values in source_protocol["retrieval_lexicon"].values() for term in values]
    pattern = "|".join(re.escape(term) for term in terms)
    data = data.loc[data["title"].fillna("").str.contains(pattern, case=False, regex=True)]
    data = data.drop_duplicates("normalized_title").copy()
    data["sample_rank"] = data["normalized_title"].map(
        lambda value: hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    )
    sample = data.sort_values("sample_rank").head(30).copy()
    labels = pd.read_csv(artifacts / "manual_labels.csv")
    if labels["title"].duplicated().any() or len(labels) != 30:
        raise ValueError("manual audit must contain 30 unique titles")
    if set(sample["title"]) != set(labels["title"]):
        missing = set(sample["title"]).difference(labels["title"])
        extra = set(labels["title"]).difference(sample["title"])
        raise ValueError(f"manual labels differ from deterministic sample; missing={missing}, extra={extra}")
    audited = sample[["pub_time", "requested_source", "title", "sample_rank"]].merge(
        labels, on="title", how="left", validate="one_to_one"
    )
    relevant = audited["label"].eq("CATALYST_RELEVANT")
    precision = float(relevant.mean())
    passed = precision >= float(protocol["minimum_precision"])
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "retrieved_title_universe": int(len(data)),
        "audited_titles": int(len(audited)),
        "relevant_titles": int(relevant.sum()),
        "semantic_precision": precision,
        "minimum_precision": float(protocol["minimum_precision"]),
        "passed": passed,
        "reads_post_event_prices": False,
        "decision": "PROCEED_TO_FULL_NEWS_BACKFILL" if passed else "REDESIGN_SEMANTIC_EXTRACTION_BEFORE_FULL_BACKFILL",
    }
    audited.to_csv(artifacts / "audited_sample.csv", index=False, lineterminator="\n")
    (artifacts / "semantic_audit_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX35 执行\n\n"
        f"状态：`COMPLETE`。标题词典在新浪财经样本中召回{len(data)}个去重标题；"
        f"按SHA-256固定抽取30条，人工判定{int(relevant.sum())}条是可用催化，"
        f"语义精确率{precision:.2%}。未读取任何行情。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX35 结论\n\n"
        f"裁决：`{evidence['decision']}`。当前“标题关键词”只达到{precision:.2%}精确率，"
        f"低于{float(protocol['minimum_precision']):.0%}门槛。禁止全量回填和收益检验；"
        "如继续，必须建立实体、事件动作与市场复述排除的事件本体，并用独立留出样本验证。\n",
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
