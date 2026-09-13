from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX34"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _fetch_window(
    pro: object,
    source: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    fields: str,
    split_rows: int,
    minimum_minutes: int,
    counters: dict[str, object],
) -> list[pd.DataFrame]:
    frame = pro.major_news(
        src=source,
        start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
        end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
        fields=fields,
    )
    counters["api_calls"] = int(counters["api_calls"]) + 1
    data = pd.DataFrame() if frame is None else pd.DataFrame(frame)
    duration_minutes = (end - start).total_seconds() / 60.0
    if len(data) >= split_rows:
        if duration_minutes <= minimum_minutes:
            counters["unsplittable_saturated_chunks"] = int(counters["unsplittable_saturated_chunks"]) + 1
            counters["terminal_chunk_rows"].append(int(len(data)))
            return [data]
        midpoint = start + (end - start) / 2
        return _fetch_window(pro, source, start, midpoint, fields, split_rows, minimum_minutes, counters) + _fetch_window(
            pro, source, midpoint + pd.Timedelta(seconds=1), end, fields, split_rows, minimum_minutes, counters
        )
    counters["terminal_chunk_rows"].append(int(len(data)))
    return [data]


def _normalized_title(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).lower()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("reads_post_event_prices", "event_generation", "direction_classification")):
        raise ValueError("EX34 is a data and semantic feasibility gate only")

    pro = get_tushare_pro(repo / ".env")
    extraction = protocol["adaptive_extraction"]
    all_frames: list[pd.DataFrame] = []
    quality_rows: list[dict[str, object]] = []
    for source in protocol["source"]["sources"]:
        for sample_date in protocol["sample_dates"]:
            start = pd.Timestamp(sample_date)
            end = start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            counters: dict[str, object] = {
                "api_calls": 0,
                "terminal_chunk_rows": [],
                "unsplittable_saturated_chunks": 0,
            }
            parts = _fetch_window(
                pro,
                str(source),
                start,
                end,
                str(protocol["source"]["fields"]),
                int(extraction["split_at_or_above_rows"]),
                int(extraction["minimum_window_minutes"]),
                counters,
            )
            data = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
            required = ["title", "content", "pub_time", "src"]
            missing = sorted(set(required).difference(data.columns))
            if missing:
                raise ValueError(f"major_news response missing fields: {missing}")
            data["pub_time"] = pd.to_datetime(data["pub_time"], errors="coerce")
            data["sample_date"] = sample_date
            data["requested_source"] = source
            valid_time = data["pub_time"].between(start, end, inclusive="both")
            core_complete = data[["title", "pub_time", "src"]].notna().all(axis=1)
            quality_rows.append(
                {
                    "source": source,
                    "sample_date": sample_date,
                    "rows_before_deduplication": int(len(data)),
                    "rows_within_requested_date": int(valid_time.sum()),
                    "core_field_coverage": float(core_complete.mean()) if len(data) else 0.0,
                    "api_calls": int(counters["api_calls"]),
                    "maximum_terminal_chunk_rows": max(counters["terminal_chunk_rows"], default=0),
                    "unsplittable_saturated_chunks": int(counters["unsplittable_saturated_chunks"]),
                }
            )
            all_frames.append(data)

    raw = pd.concat(all_frames, ignore_index=True)
    raw = raw.drop_duplicates(["requested_source", "pub_time", "title"], keep="first")
    raw["normalized_title"] = raw["title"].map(_normalized_title)
    raw["content_present"] = raw["content"].fillna("").astype(str).str.strip().ne("")
    raw = raw.sort_values(["pub_time", "requested_source", "title"]).reset_index(drop=True)
    combined = raw["title"].fillna("").astype(str) + "\n" + raw["content"].fillna("").astype(str)
    domain_columns: list[str] = []
    for domain, terms in protocol["retrieval_lexicon"].items():
        column = f"domain_{str(domain).lower()}"
        pattern = "|".join(re.escape(str(term)) for term in terms)
        raw[column] = combined.str.contains(pattern, case=False, regex=True)
        domain_columns.append(column)
    raw["retrieved_for_manual_audit"] = raw[domain_columns].any(axis=1)
    hits = raw.loc[
        raw["retrieved_for_manual_audit"],
        ["pub_time", "requested_source", "title", "content", "normalized_title", *domain_columns],
    ].copy()
    quality = pd.DataFrame(quality_rows)
    gate = protocol["quality_gate"]
    source_summary = quality.groupby("source", observed=True).agg(
        sample_dates_with_data=("rows_before_deduplication", lambda values: int(values.gt(0).sum())),
        sample_dates=("sample_date", "nunique"),
        rows=("rows_before_deduplication", "sum"),
        api_calls=("api_calls", "sum"),
        minimum_core_field_coverage=("core_field_coverage", "min"),
        maximum_terminal_chunk_rows=("maximum_terminal_chunk_rows", "max"),
        unsplittable_saturated_chunks=("unsplittable_saturated_chunks", "sum"),
    ).reset_index()
    source_summary["sample_date_coverage"] = source_summary["sample_dates_with_data"] / source_summary["sample_dates"]
    source_summary["technical_pass"] = (
        source_summary["sample_date_coverage"].ge(float(gate["minimum_source_sample_date_coverage"]))
        & source_summary["minimum_core_field_coverage"].ge(float(gate["minimum_core_field_coverage"]))
        & source_summary["maximum_terminal_chunk_rows"].lt(int(gate["maximum_terminal_chunk_rows_exclusive"]))
        & source_summary["unsplittable_saturated_chunks"].eq(0)
    )
    domain_counts = {column.removeprefix("domain_").upper(): int(hits[column].sum()) for column in domain_columns}
    semantic_checks = {
        "minimum_retrieved_titles": int(len(hits)) >= int(gate["minimum_retrieved_titles"]),
        "minimum_retrieved_sample_dates": int(hits["pub_time"].dt.strftime("%Y-%m-%d").nunique()) >= int(gate["minimum_retrieved_sample_dates"]),
        "minimum_each_domain": all(count >= int(gate["minimum_retrieved_titles_per_domain"]) for count in domain_counts.values()),
    }
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "sample_rows": int(len(raw)),
        "sample_content_coverage": float(raw["content_present"].mean()),
        "retrieved_titles": int(len(hits)),
        "retrieved_sample_dates": int(hits["pub_time"].dt.strftime("%Y-%m-%d").nunique()),
        "domain_counts": domain_counts,
        "technical_source_pass": {str(row.source): bool(row.technical_pass) for row in source_summary.itertuples(index=False)},
        "semantic_pilot_checks": semantic_checks,
        "reads_post_event_prices": False,
    }
    evidence["decision"] = (
        "PROCEED_TO_MANUAL_SEMANTIC_AUDIT"
        if source_summary["technical_pass"].any() and all(semantic_checks.values())
        else "STOP_OR_REDESIGN_UNSTRUCTURED_NEWS_ROUTE"
    )
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    raw.drop(columns=["content"]).to_csv(
        artifacts / "news_sample_metadata.csv.gz", index=False, compression=compression, lineterminator="\n"
    )
    hits.to_csv(artifacts / "semantic_audit_sample.csv.gz", index=False, compression=compression, lineterminator="\n")
    quality.to_csv(artifacts / "sample_quality.csv", index=False, lineterminator="\n")
    source_summary.to_csv(artifacts / "source_summary.csv", index=False, lineterminator="\n")
    (artifacts / "feasibility_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    rows = ["|来源|样本日覆盖|样本行|调用数|最大终端分片|技术门|", "|---|---:|---:|---:|---:|---|"]
    for row in source_summary.itertuples(index=False):
        rows.append(
            f"|{row.source}|{row.sample_dates_with_data}/{row.sample_dates}|{row.rows}|{row.api_calls}|"
            f"{row.maximum_terminal_chunk_rows}|{'PASS' if row.technical_pass else 'FAIL'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX34 执行\n\n状态：`COMPLETE`。未读取任何事件后价格。\n\n"
        + "\n".join(rows)
        + f"\n\n词典召回{len(hits)}条，覆盖{evidence['retrieved_sample_dates']}个抽样日；分类计数："
        + "、".join(f"{name}={count}" for name, count in domain_counts.items())
        + "。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX34 结论\n\n"
        f"裁决：`{evidence['decision']}`。这一裁决最多只允许进入人工语义审核，"
        "不代表新闻可交易，不允许读取收益、创建候选或修改SM/PTE。\n",
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
