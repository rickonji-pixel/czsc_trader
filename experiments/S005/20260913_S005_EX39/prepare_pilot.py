from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


def _fetch_window(pro: object, source: str, start: pd.Timestamp, end: pd.Timestamp, fields: str) -> list[pd.DataFrame]:
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


def _normalized_title(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).lower()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    if protocol.get("semantic_program_filter") or protocol.get("reads_post_event_prices"):
        raise ValueError("EX39 permits acquisition and mechanical deduplication only")

    pro = get_tushare_pro(repo / ".env")
    parts: list[pd.DataFrame] = []
    for sample_date in protocol["sample_dates"]:
        start = pd.Timestamp(sample_date)
        end = start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        parts.extend(_fetch_window(pro, protocol["source"]["source"], start, end, protocol["source"]["fields"]))
    raw = pd.concat(parts, ignore_index=True)
    required = {"title", "content", "pub_time", "src"}
    if not required.issubset(raw.columns):
        raise ValueError(f"major_news response missing fields: {sorted(required.difference(raw.columns))}")
    raw["content"] = raw["content"].fillna("").astype(str)
    raw["pub_time"] = pd.to_datetime(raw["pub_time"], errors="raise")
    raw["normalized_title"] = raw["title"].map(_normalized_title)
    raw = raw.sort_values("pub_time").drop_duplicates("normalized_title", keep="first").reset_index(drop=True)
    raw.insert(0, "sample_id", [f"EX39-{value:04d}" for value in range(1, len(raw) + 1)])
    raw["raw_sha256"] = raw.apply(
        lambda row: hashlib.sha256(
            f"{row['src']}\n{row['pub_time'].isoformat()}\n{row['title']}\n{row['content']}".encode("utf-8")
        ).hexdigest(),
        axis=1,
    )
    cache_path = repo / protocol["local_cache"]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    raw[["sample_id", "pub_time", "src", "title", "content", "raw_sha256"]].to_csv(
        cache_path, index=False, compression=compression, lineterminator="\n"
    )
    pilot = raw.head(int(protocol["manual_pilot"]["rows"])).copy()
    pilot[["sample_id", "pub_time", "src", "title", "raw_sha256"]].to_csv(
        artifacts / "article_index.csv", index=False, lineterminator="\n"
    )
    pd.DataFrame(
        {
            "sample_id": pilot["sample_id"],
            "relevance": "",
            "review_evidence_excerpt": "",
            "review_reason": "",
        }
    ).to_csv(artifacts / "manual_labels.csv", index=False, lineterminator="\n")
    pd.DataFrame(
        columns=[
            "event_id", "sample_id", "published_at", "primary_entity", "event_type", "direction",
            "event_summary", "evidence_excerpt", "duplicate_event_key",
        ]
    ).to_csv(artifacts / "manual_events.csv", index=False, lineterminator="\n")
    manifest = {
        "schema_version": 1,
        "experiment_id": protocol["experiment_id"],
        "cache_path": protocol["local_cache"],
        "cache_sha256": raw_file_sha256(cache_path),
        "article_rows": int(len(raw)),
        "manual_pilot_rows": int(len(pilot)),
        "manual_pilot_first_pub_time": str(pilot["pub_time"].min()),
        "manual_pilot_last_pub_time": str(pilot["pub_time"].max()),
        "content_nonempty_rows": int(raw["content"].str.strip().ne("").sum()),
        "sample_dates": protocol["sample_dates"],
        "semantic_program_filter": False,
        "reads_post_event_prices": False,
    }
    (artifacts / "cache_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
