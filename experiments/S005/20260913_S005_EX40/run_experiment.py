from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX40"


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_post_event_prices"):
        raise ValueError("EX40 is a no-return preregistration experiment")

    frames: list[pd.DataFrame] = []
    cache_paths = {
        "20260913_S005_EX36": repo / ".tmp/research_cache/S005/news/ex36_sina_sample.csv.gz",
        "20260913_S005_EX38": repo / ".tmp/research_cache/S005/news/ex38_sina_holdout_v2.csv.gz",
        "20260913_S005_EX39": repo / ".tmp/research_cache/S005/news/ex39_sina_full_pilot.csv.gz",
    }
    for source in protocol["sources"]:
        source_id = source["experiment_id"]
        source_path = repo / "experiments/S005" / source_id
        validate_experiment_archive(source_path)
        if raw_file_sha256(source_path / "experiment_manifest.json") != source["manifest_sha256"]:
            raise ValueError(f"source manifest differs: {source_id}")
        events = pd.read_csv(source_path / "artifacts/manual_events.csv", keep_default_na=False)
        if "published_at" not in events.columns:
            raw = pd.read_csv(cache_paths[source_id], keep_default_na=False)
            events = events.merge(raw[["sample_id", "pub_time"]], on="sample_id", validate="many_to_one")
            events = events.rename(columns={"pub_time": "published_at"})
        events["source_experiment"] = source_id
        frames.append(events)
    corpus = pd.concat(frames, ignore_index=True)
    corpus["published_at"] = pd.to_datetime(corpus["published_at"], errors="raise")
    corpus = corpus.sort_values(["published_at", "source_experiment", "event_id"])
    unique = corpus.drop_duplicates("duplicate_event_key", keep="first").copy()
    positive = unique.loc[unique["direction"].eq("POSITIVE")].copy()
    positive["signal_date"] = positive["published_at"].dt.normalize()
    signal_dates = positive.groupby("signal_date", observed=True).agg(
        unique_positive_events=("duplicate_event_key", "size"),
        entities=("primary_entity", lambda values: "|".join(sorted(set(map(str, values))))),
        event_types=("event_type", lambda values: "|".join(sorted(set(map(str, values))))),
    ).reset_index()
    if len(signal_dates) < 1:
        raise ValueError("manual corpus contains no positive signal dates")

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    corpus.to_csv(artifacts / "manual_event_corpus.csv.gz", index=False, compression=compression, lineterminator="\n")
    unique.to_csv(artifacts / "unique_events.csv", index=False, lineterminator="\n")
    signal_dates.to_csv(artifacts / "frozen_signal_dates.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "event_rows": int(len(corpus)),
        "unique_events": int(len(unique)),
        "event_dates": int(unique["published_at"].dt.normalize().nunique()),
        "positive_unique_events": int(len(positive)),
        "frozen_signal_dates": int(len(signal_dates)),
        "first_signal_date": str(signal_dates["signal_date"].min().date()),
        "last_signal_date": str(signal_dates["signal_date"].max().date()),
        "temporal_coverage_sufficient_for_candidate_evaluation": False,
        "reads_post_event_prices": False,
        "decision": "PREREGISTER_DIRECTIONAL_NEWS_TEST_ONLY",
    }
    (artifacts / "selection_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX40 执行\n\n"
        f"状态：`COMPLETE`。合并人工事件{len(corpus)}条，得到{len(unique)}个去重事实、"
        f"{unique['published_at'].dt.normalize().nunique()}个新闻日期；其中正向去重事实{len(positive)}个，"
        f"按日合并后冻结{len(signal_dates)}个方向性测试信号。未读取事件后行情或收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX40 结论\n\n"
        "裁决：`PREREGISTER_DIRECTIONAL_NEWS_TEST_ONLY`。人工事件链路可用于提出可证伪假设，但时间覆盖"
        "不足以评估频率、年度稳定性或候选资格。下一实验只能原样执行通用正向新闻延续的方向性测试；"
        "不得依据结果拆分事件类型、公司、年份或持有期。\n",
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
