from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX36"
SALT = "S005_EX36_V1"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _normalized_title(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).lower()


def _fetch_window(
    pro: object,
    source: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    fields: str,
) -> list[pd.DataFrame]:
    frame = pro.major_news(
        src=source,
        start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
        end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
        fields=fields,
    )
    data = pd.DataFrame() if frame is None else pd.DataFrame(frame)
    if len(data) < 350:
        return [data]
    if (end - start).total_seconds() <= 30 * 60:
        raise RuntimeError(f"major_news remains saturated at minimum window: {start} - {end}")
    midpoint = start + (end - start) / 2
    return _fetch_window(pro, source, start, midpoint, fields) + _fetch_window(
        pro, source, midpoint + pd.Timedelta(seconds=1), end, fields
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices") or protocol.get("rule_evaluation"):
        raise ValueError("EX36 may not read returns or evaluate extraction rules")

    ex34 = repo / "experiments/S005/20260913_S005_EX34"
    ex35 = repo / "experiments/S005/20260913_S005_EX35"
    validate_experiment_archive(ex34)
    validate_experiment_archive(ex35)
    source = protocol["source"]
    expected = {
        ex34 / "experiment_manifest.json": source["ex34_manifest_sha256"],
        ex34 / "artifacts/news_sample_metadata.csv.gz": source["ex34_metadata_sha256"],
        ex35 / "experiment_manifest.json": source["ex35_manifest_sha256"],
        ex35 / "artifacts/manual_labels.csv": source["ex35_labels_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")

    ex34_protocol = _read(ex34 / "artifacts/protocol.json")
    pro = get_tushare_pro(repo / ".env")
    parts: list[pd.DataFrame] = []
    for sample_date in ex34_protocol["sample_dates"]:
        start = pd.Timestamp(str(sample_date))
        end = start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        parts.extend(_fetch_window(pro, str(source["source"]), start, end, str(source["fields"])))
    raw = pd.concat(parts, ignore_index=True)
    required = ["title", "content", "pub_time", "src"]
    missing = sorted(set(required).difference(raw.columns))
    if missing:
        raise ValueError(f"major_news response missing fields: {missing}")
    raw["pub_time"] = pd.to_datetime(raw["pub_time"], errors="raise")
    raw = raw.drop_duplicates(["pub_time", "title"], keep="first")
    raw["normalized_title"] = raw["title"].map(_normalized_title)
    raw = raw.drop_duplicates("normalized_title", keep="first").copy()
    raw["content"] = raw["content"].fillna("").astype(str)
    raw["raw_sha256"] = raw.apply(
        lambda row: hashlib.sha256(
            f"{row['src']}\n{row['pub_time'].isoformat()}\n{row['title']}\n{row['content']}".encode("utf-8")
        ).hexdigest(),
        axis=1,
    )

    terms = [str(term) for values in ex34_protocol["retrieval_lexicon"].values() for term in values]
    pattern = "|".join(re.escape(term) for term in terms)
    raw["title_lexicon_hit"] = raw["title"].fillna("").str.contains(pattern, case=False, regex=True)
    excluded = {
        _normalized_title(title)
        for title in pd.read_csv(ex35 / "artifacts/manual_labels.csv")["title"].astype(str)
    }
    pool = raw.loc[~raw["normalized_title"].isin(excluded)].copy()
    pool["bucket"] = pool["title_lexicon_hit"].map(
        {True: "TITLE_LEXICON_HIT", False: "TITLE_LEXICON_NON_HIT"}
    )
    pool["sample_rank"] = pool.apply(
        lambda row: hashlib.sha256(
            f"{SALT}|{row['bucket']}|{row['normalized_title']}".encode("utf-8")
        ).hexdigest(),
        axis=1,
    )
    selected_parts: list[pd.DataFrame] = []
    for bucket, count in protocol["holdout"]["buckets"].items():
        candidates = pool.loc[pool["bucket"].eq(bucket)].sort_values("sample_rank")
        if len(candidates) < int(count):
            raise ValueError(f"insufficient holdout candidates for {bucket}: {len(candidates)} < {count}")
        selected_parts.append(candidates.head(int(count)))
    selected = pd.concat(selected_parts, ignore_index=True).sort_values(["bucket", "sample_rank"]).reset_index(drop=True)
    selected.insert(0, "sample_id", [f"EX36-{value:03d}" for value in range(1, len(selected) + 1)])

    labels_path = artifacts / "manual_labels.csv"
    index_path = artifacts / "holdout_index.csv"
    labels_completed = (
        labels_path.exists()
        and pd.read_csv(labels_path, keep_default_na=False)["relevance"].ne("").any()
    )
    if labels_completed:
        if not index_path.exists():
            raise ValueError("completed manual labels exist without frozen holdout index")
        frozen_index = pd.read_csv(index_path, keep_default_na=False)
        regenerated = selected[["sample_id", "raw_sha256"]].astype(str)
        frozen = frozen_index[["sample_id", "raw_sha256"]].astype(str)
        if not frozen.equals(regenerated):
            raise ValueError("completed manual labels differ from regenerated holdout")

    cache_path = repo / str(protocol["local_cache"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    selected.to_csv(cache_path, index=False, compression=compression, lineterminator="\n")
    index_columns = [
        "sample_id", "bucket", "sample_rank", "pub_time", "src", "title", "raw_sha256",
    ]
    selected[index_columns].to_csv(index_path, index=False, lineterminator="\n")
    label_columns = ["sample_id", "relevance", "review_evidence_excerpt", "review_reason"]
    template = selected[["sample_id"]].copy()
    for column in label_columns[1:]:
        template[column] = ""
    if not labels_completed:
        template.to_csv(labels_path, index=False, lineterminator="\n")
    events_path = artifacts / "manual_events.csv"
    if not events_path.exists():
        pd.DataFrame(
            columns=[
                "event_id", "sample_id", "entity_scope", "primary_entity", "event_type", "direction",
                "event_summary", "evidence_excerpt", "duplicate_event_key",
            ]
        ).to_csv(events_path, index=False, lineterminator="\n")
    cache_manifest = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "cache_path": str(protocol["local_cache"]),
        "cache_sha256": raw_file_sha256(cache_path),
        "rows": int(len(selected)),
        "bucket_counts": {str(key): int(value) for key, value in selected["bucket"].value_counts().items()},
        "content_nonempty": int(selected["content"].str.strip().ne("").sum()),
        "reads_post_event_prices": False,
    }
    (artifacts / "cache_manifest.json").write_text(
        json.dumps(cache_manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(cache_manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
