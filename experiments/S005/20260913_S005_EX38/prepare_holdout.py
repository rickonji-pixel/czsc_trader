from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX38"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _normalized_title(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).lower()


def _load_prefilter(path: Path):
    spec = importlib.util.spec_from_file_location("s005_ex37_frozen_prefilter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen prefilter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_post_event_prices"):
        raise ValueError("invalid EX38 protocol")

    ex34 = repo / "experiments/S005/20260913_S005_EX34"
    ex35 = repo / "experiments/S005/20260913_S005_EX35"
    ex36 = repo / "experiments/S005/20260913_S005_EX36"
    ex37 = repo / "experiments/S005/20260913_S005_EX37"
    for source in (ex34, ex35, ex36, ex37):
        validate_experiment_archive(source)
    expected = {
        ex34 / "experiment_manifest.json": protocol["source"]["ex34_manifest_sha256"],
        ex35 / "artifacts/manual_labels.csv": protocol["source"]["ex35_labels_sha256"],
        ex36 / "artifacts/holdout_index.csv": protocol["source"]["ex36_index_sha256"],
        ex37 / "experiment_manifest.json": protocol["frozen_prefilter"]["ex37_manifest_sha256"],
        ex37 / "prefilter.py": protocol["frozen_prefilter"]["code_sha256"],
        ex37 / "artifacts/important_component_entities.csv": protocol["frozen_prefilter"]["entity_snapshot_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"frozen source hash differs: {path}")

    pro = get_tushare_pro(repo / ".env")
    parts: list[pd.DataFrame] = []
    for sample_date in protocol["holdout"]["sample_dates"]:
        start = pd.Timestamp(str(sample_date))
        end = start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        parts.extend(_fetch_window(pro, protocol["source"]["source"], start, end, protocol["source"]["fields"]))
    raw = pd.concat(parts, ignore_index=True)
    required = {"title", "content", "pub_time", "src"}
    if not required.issubset(raw.columns):
        raise ValueError(f"major_news response missing fields: {sorted(required.difference(raw.columns))}")
    raw["pub_time"] = pd.to_datetime(raw["pub_time"], errors="raise")
    raw["content"] = raw["content"].fillna("").astype(str)
    raw["normalized_title"] = raw["title"].map(_normalized_title)
    raw = raw.sort_values("pub_time").drop_duplicates("normalized_title", keep="first").copy()
    raw["raw_sha256"] = raw.apply(
        lambda row: hashlib.sha256(
            f"{row['src']}\n{row['pub_time'].isoformat()}\n{row['title']}\n{row['content']}".encode("utf-8")
        ).hexdigest(),
        axis=1,
    )
    aborted_index = artifacts / "aborted_blind_order_index.csv"
    if raw_file_sha256(aborted_index) != protocol["holdout"]["aborted_v1_index_sha256"]:
        raise ValueError("aborted blind-order evidence hash differs")
    read_titles = {
        _normalized_title(value)
        for value in pd.concat(
            [
                pd.read_csv(ex35 / "artifacts/manual_labels.csv")["title"],
                pd.read_csv(ex36 / "artifacts/holdout_index.csv")["title"],
                pd.read_csv(aborted_index)["title"],
            ],
            ignore_index=True,
        ).astype(str)
    }
    pool = raw.loc[~raw["normalized_title"].isin(read_titles)].copy()

    module = _load_prefilter(ex37 / "prefilter.py")
    entities = pd.read_csv(ex37 / "artifacts/important_component_entities.csv")
    important_names = tuple(sorted(entities["name"].astype(str).unique(), key=lambda value: (-len(value), value)))
    predictions = []
    for row in pool.itertuples(index=False):
        result = module.select_news_candidate(row.title, row.content, important_names)
        predictions.append((bool(result.selected), result.route))
    pool[["selected", "route"]] = pd.DataFrame(predictions, index=pool.index)
    pool["stratum"] = pool["selected"].map({True: "PREFILTER_SELECTED", False: "PREFILTER_NOT_SELECTED"})
    salt = str(protocol["holdout"]["salt"])
    pool["sample_rank"] = pool.apply(
        lambda row: hashlib.sha256(f"{salt}|{row['stratum']}|{row['normalized_title']}".encode("utf-8")).hexdigest(),
        axis=1,
    )
    requests = {
        "PREFILTER_SELECTED": int(pool["selected"].sum()),
        "PREFILTER_NOT_SELECTED": int(protocol["holdout"]["not_selected_sample_rows"]),
    }
    chosen = []
    pool_counts: dict[str, int] = {}
    for stratum, count in requests.items():
        candidates = pool.loc[pool["stratum"].eq(stratum)].sort_values("sample_rank")
        pool_counts[stratum] = int(len(candidates))
        if len(candidates) < count:
            raise ValueError(f"insufficient rows for {stratum}: {len(candidates)} < {count}")
        chosen.append(candidates.head(count))
    selected = pd.concat(chosen, ignore_index=True)
    selected["blind_rank"] = selected["normalized_title"].map(
        lambda value: hashlib.sha256(f"{salt}|BLIND|{value}".encode("utf-8")).hexdigest()
    )
    selected = selected.sort_values("blind_rank").reset_index(drop=True)
    selected.insert(0, "sample_id", [f"EX38-{value:03d}" for value in range(1, len(selected) + 1)])

    labels_path = artifacts / "manual_labels.csv"
    index_path = artifacts / "holdout_index.csv"
    labels_completed = labels_path.exists() and pd.read_csv(labels_path, keep_default_na=False)["relevance"].ne("").any()
    if labels_completed:
        frozen = pd.read_csv(index_path, keep_default_na=False)[["sample_id", "raw_sha256"]].astype(str)
        regenerated = selected[["sample_id", "raw_sha256"]].astype(str)
        if not frozen.equals(regenerated):
            raise ValueError("completed manual labels differ from regenerated holdout")

    cache_path = repo / protocol["local_cache"]
    sealed_path = repo / protocol["sealed_predictions"]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    public_columns = ["sample_id", "blind_rank", "pub_time", "src", "title", "content", "raw_sha256"]
    selected[public_columns].to_csv(cache_path, index=False, compression=compression, lineterminator="\n")
    selected[["sample_id", "selected", "route"]].to_csv(
        sealed_path, index=False, compression=compression, lineterminator="\n"
    )
    selected[["sample_id", "blind_rank", "pub_time", "src", "title", "raw_sha256"]].to_csv(
        index_path, index=False, lineterminator="\n"
    )
    if not labels_completed:
        pd.DataFrame(
            {
                "sample_id": selected["sample_id"],
                "relevance": "",
                "review_evidence_excerpt": "",
                "review_reason": "",
            }
        ).to_csv(labels_path, index=False, lineterminator="\n")
    events_path = artifacts / "manual_events.csv"
    if not events_path.exists():
        pd.DataFrame(
            columns=[
                "event_id", "sample_id", "entity_scope", "primary_entity", "event_type", "direction",
                "event_summary", "evidence_excerpt", "duplicate_event_key",
            ]
        ).to_csv(events_path, index=False, lineterminator="\n")
    manifest = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "cache_path": protocol["local_cache"],
        "cache_sha256": raw_file_sha256(cache_path),
        "sealed_predictions_path": protocol["sealed_predictions"],
        "sealed_predictions_sha256": raw_file_sha256(sealed_path),
        "rows": int(len(selected)),
        "pool_counts": pool_counts,
        "sample_counts": requests,
        "content_nonempty": int(selected["content"].str.strip().ne("").sum()),
        "predictions_remain_blinded_until_manual_review_complete": True,
        "reads_post_event_prices": False,
    }
    (artifacts / "cache_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in manifest.items() if "sealed" not in key}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
