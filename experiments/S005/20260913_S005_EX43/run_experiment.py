from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENT_ID = "20260913_S005_EX43"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _request(method, **kwargs) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            value = method(**kwargs)
            return pd.DataFrame() if value is None else pd.DataFrame(value)
        except Exception as error:  # pragma: no cover - external retry
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _normalise(frame: pd.DataFrame, source: str, code: str) -> pd.DataFrame:
    value = frame.copy()
    if value.empty:
        return value
    required = {"ts_code", "ann_date", "end_date"}
    missing = sorted(required.difference(value.columns))
    if missing:
        raise ValueError(f"{source} {code} missing fields: {missing}")
    value["source"] = source
    value["source_row"] = np.arange(len(value), dtype=int)
    return value


def _first_later_session(calendar: np.ndarray, value: pd.Timestamp) -> pd.Timestamp | pd.NaT:
    position = int(np.searchsorted(calendar, value.to_datetime64(), side="right"))
    if position >= len(calendar):
        return pd.NaT
    return pd.Timestamp(calendar[position])


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    sys.path.insert(0, str(repo / "src"))
    sys.path.insert(0, str(repo / "packages" / "dataflows" / "src"))
    from czsc_trader.experiment_archive import (  # noqa: PLC0415
        build_experiment_manifest,
        validate_experiment_archive,
    )
    from czsc_trader.identity import raw_file_sha256  # noqa: PLC0415
    from dataflows.tushare_common import get_tushare_pro  # noqa: PLC0415

    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("reads_post_event_prices", "event_generation", "parameter_selection")
    ):
        raise ValueError("EX43 may only validate disclosure source data")

    entity_spec = protocol["entities"]
    entity_path = repo / str(entity_spec["path"])
    if raw_file_sha256(entity_path) != entity_spec["sha256"]:
        raise ValueError("important constituent universe differs from frozen protocol")
    entities = pd.read_csv(entity_path, dtype={"con_code": str})
    if len(entities) != int(entity_spec["expected_count"]):
        raise ValueError("important constituent count differs from frozen protocol")

    membership_spec = protocol["membership_panel"]
    membership_path = repo / str(membership_spec["path"])
    if raw_file_sha256(membership_path) != membership_spec["sha256"]:
        raise ValueError("point-in-time membership panel differs from frozen protocol")
    membership = pd.read_csv(
        membership_path,
        usecols=["dt", "con_code", "weight"],
        parse_dates=["dt"],
    )
    membership["dt"] = membership["dt"].dt.normalize()
    membership["con_code"] = membership["con_code"].astype(str)
    calendar = np.sort(membership["dt"].unique())
    member_keys = set(zip(membership["dt"], membership["con_code"], strict=False))

    start = pd.Timestamp(protocol["development_start"])
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    pro = get_tushare_pro(repo / ".env")
    source_frames: dict[str, list[pd.DataFrame]] = {
        source: [] for source in protocol["sources"]
    }
    request_failures: list[dict[str, str]] = []
    successful_requests = 0
    api_calls = 0
    for position, code in enumerate(entities["con_code"], start=1):
        for source in protocol["sources"]:
            api_calls += 1
            try:
                frame = _request(
                    getattr(pro, source),
                    ts_code=code,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=cutoff.strftime("%Y%m%d"),
                    fields=str(protocol["source_fields"][source]),
                )
                source_frames[source].append(_normalise(frame, source, code))
                successful_requests += 1
            except Exception as error:
                request_failures.append(
                    {"ts_code": code, "source": source, "error": str(error)}
                )
        if position % 8 == 0 or position == len(entities):
            print(f"entities {position}/{len(entities)}", flush=True)

    combined_frames: list[pd.DataFrame] = []
    source_counts: dict[str, int] = {}
    exact_duplicate_rows: dict[str, int] = {}
    key_duplicate_rows: dict[str, int] = {}
    for source, frames in source_frames.items():
        nonempty = [frame for frame in frames if not frame.empty]
        value = pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()
        source_counts[source] = int(len(value))
        comparison_columns = [column for column in value.columns if column != "source_row"]
        exact_duplicate_rows[source] = int(
            value.duplicated(comparison_columns, keep=False).sum()
        )
        key_duplicate_rows[source] = int(
            value.duplicated(["ts_code", "ann_date", "end_date"], keep=False).sum()
        ) if not value.empty else 0
        value.to_csv(
            artifacts / f"{source}_raw.csv.gz",
            index=False,
            compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
            lineterminator="\n",
        )
        if not value.empty:
            combined_frames.append(value)

    if not combined_frames:
        raise ValueError("Tushare returned no disclosure data")
    raw = pd.concat(combined_frames, ignore_index=True, sort=False)
    raw["ann_date_parsed"] = pd.to_datetime(raw["ann_date"], format="%Y%m%d", errors="coerce")
    raw["end_date_parsed"] = pd.to_datetime(raw["end_date"], format="%Y%m%d", errors="coerce")
    raw["eligible_session"] = raw["ann_date_parsed"].map(
        lambda item: _first_later_session(calendar, item) if pd.notna(item) else pd.NaT
    )
    raw["point_in_time_member"] = [
        (session, code) in member_keys if pd.notna(session) else False
        for session, code in zip(raw["eligible_session"], raw["ts_code"], strict=False)
    ]
    performance_columns = [
        column
        for column in (
            "q_sales_yoy",
            "netprofit_yoy",
            "dt_netprofit_yoy",
            "q_op_qoq",
            "p_change_min",
            "p_change_max",
            "revenue",
            "n_income",
            "yoy_net_profit",
        )
        if column in raw.columns
    ]
    numeric = raw[performance_columns].apply(pd.to_numeric, errors="coerce")
    raw["has_performance_value"] = numeric.notna().any(axis=1)

    source_priority = {"forecast": 0, "express": 1, "fina_indicator": 2}
    raw["source_priority"] = raw["source"].map(source_priority)
    first = (
        raw.sort_values(
            ["ts_code", "end_date_parsed", "ann_date_parsed", "source_priority", "source_row"]
        )
        .drop_duplicates(["ts_code", "end_date_parsed"], keep="first")
        .copy()
    )
    first_out = first[
        [
            "ts_code",
            "end_date_parsed",
            "ann_date_parsed",
            "source",
            "eligible_session",
            "point_in_time_member",
            "has_performance_value",
        ]
    ].rename(
        columns={
            "end_date_parsed": "end_date",
            "ann_date_parsed": "ann_date",
            "source": "first_source",
        }
    )
    for column in ("end_date", "ann_date", "eligible_session"):
        first_out[column] = pd.to_datetime(first_out[column]).dt.strftime("%Y-%m-%d")
    first_out.to_csv(artifacts / "first_disclosures.csv", index=False, lineterminator="\n")

    required_dates = raw[["ann_date_parsed", "end_date_parsed"]].notna().all(axis=1)
    in_period = raw["ann_date_parsed"].between(start, cutoff, inclusive="both")
    first_member = first[first["point_in_time_member"] & first["eligible_session"].notna()]
    gate = protocol["quality_gate"]
    entities_with_data = int(raw["ts_code"].nunique())
    performance_coverage = float(first_member["has_performance_value"].mean())
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "entity_count": int(len(entities)),
        "entities_with_data": entities_with_data,
        "successful_requests": successful_requests,
        "request_failures": request_failures,
        "api_calls": api_calls,
        "source_rows": source_counts,
        "exact_duplicate_rows": exact_duplicate_rows,
        "duplicate_business_key_rows": key_duplicate_rows,
        "required_date_coverage": float(required_dates.mean()),
        "all_announcements_within_development_period": bool(in_period.all()),
        "first_disclosures": int(len(first)),
        "point_in_time_member_first_disclosures": int(len(first_member)),
        "performance_value_coverage": performance_coverage,
        "availability_strictly_after_announcement": bool(
            (first_member["eligible_session"] > first_member["ann_date_parsed"]).all()
        ),
        "post_event_prices_read": False,
    }
    quality["passed"] = bool(
        successful_requests >= int(gate["minimum_successful_requests"])
        and entities_with_data >= int(gate["minimum_entities_with_data"])
        and quality["required_date_coverage"] >= float(gate["minimum_required_date_coverage"])
        and quality["all_announcements_within_development_period"]
        and quality["first_disclosures"] >= int(gate["minimum_first_disclosures"])
        and quality["point_in_time_member_first_disclosures"]
        >= int(gate["minimum_point_in_time_member_first_disclosures"])
        and performance_coverage >= float(gate["minimum_performance_value_coverage"])
        and quality["availability_strictly_after_announcement"]
        and api_calls <= int(gate["maximum_api_calls"])
    )
    _write(artifacts / "data_quality.json", quality)

    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S005 EX43 执行\n\n"
        f"数据门结果：`{status}`。{successful_requests}/{api_calls}次接口调用成功，"
        f"32家公司中{entities_with_data}家取得数据。三类源分别返回："
        f"财务指标{source_counts['fina_indicator']}行、业绩预告{source_counts['forecast']}行、"
        f"业绩快报{source_counts['express']}行。\n\n"
        f"共识别{quality['first_disclosures']}个公司-报告期首次披露，其中"
        f"{quality['point_in_time_member_first_disclosures']}个在可用日满足时点成分约束；"
        f"数值业绩字段覆盖{performance_coverage:.2%}。"
        f"重复业务键行按源记录为{key_duplicate_rows}，全部保留供后续修订审计。\n\n"
        "本轮没有读取披露后的588080行情或收益。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_FUNDAMENTAL_MECHANISM_PREREGISTRATION" if quality["passed"] else "STOP_FUNDAMENTAL_ROUTE_ON_DATA"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX43 结论\n\n"
        f"裁决：`{decision}`。"
        + (
            "重要成分财报披露数据具备进入独立机制预注册的条件。下一轮必须先冻结首次公开、业绩变化和聚合规则，再读取事件后收益。"
            if quality["passed"]
            else "数据完整性、时点正确性或样本数量未达到预注册门槛，当前停止该机制路线。"
        )
        + "本轮没有创建候选、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": str(protocol["development_cutoff"]),
            "status": status,
            "decision": decision,
            "reads_prices_or_returns": False,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
