from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX22"
FIELDS = "trade_date,ts_code,name,rzye,rqye,rzmre,rqyl,rzche,rqchl,rqmcl,rzrqye"
VALUE_COLUMNS = ["rzye", "rqye", "rzmre", "rqyl", "rzche", "rqchl", "rqmcl", "rzrqye"]


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source_spec = protocol["source"]
    panel_source = repo / "experiments/S005" / str(source_spec["panel_experiment"])
    review_source = repo / "experiments/S005" / str(source_spec["review_experiment"])
    validate_experiment_archive(panel_source)
    validate_experiment_archive(review_source)
    expected = {
        panel_source / "experiment_manifest.json": source_spec["panel_manifest_sha256"],
        panel_source / "artifacts/constituent_panel.csv.gz": source_spec["panel_sha256"],
        review_source / "experiment_manifest.json": source_spec["review_manifest_sha256"],
        review_source / "artifacts/reviewed_data_quality.json": source_spec[
            "review_quality_sha256"
        ],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read(review_source / "artifacts/reviewed_data_quality.json")["passed"]:
        raise ValueError("reviewed constituent data gate did not pass")

    membership = pd.read_csv(
        panel_source / "artifacts/constituent_panel.csv.gz",
        usecols=["dt", "con_code", "snapshot_date", "weight"],
        parse_dates=["dt", "snapshot_date"],
    )
    codes = [str(protocol["trade_symbol"]), *sorted(membership["con_code"].unique())]
    pro = get_tushare_pro(repo / ".env")
    frames: list[pd.DataFrame] = []
    for position, code in enumerate(codes, start=1):
        frame = _request(
            pro.margin_detail,
            ts_code=code,
            start_date=pd.Timestamp(protocol["development_start"]).strftime("%Y%m%d"),
            end_date=pd.Timestamp(protocol["development_cutoff"]).strftime("%Y%m%d"),
            fields=FIELDS,
        )
        if not frame.empty:
            frames.append(frame)
        if position % 20 == 0 or position == len(codes):
            print(f"margin symbols {position}/{len(codes)}", flush=True)
    if not frames:
        raise ValueError("Tushare returned no margin detail")
    margin = pd.concat(frames, ignore_index=True)
    margin["trade_date"] = pd.to_datetime(margin["trade_date"], format="%Y%m%d")
    duplicates = int(margin.duplicated(["trade_date", "ts_code"], keep=False).sum())
    margin = margin.drop_duplicates(["trade_date", "ts_code"])
    for column in VALUE_COLUMNS:
        margin[column] = pd.to_numeric(margin[column], errors="coerce")

    component = membership.merge(
        margin.rename(columns={"trade_date": "dt", "ts_code": "con_code"}),
        on=["dt", "con_code"],
        how="left",
        validate="one_to_one",
    )
    component["observed_margin"] = component["rzye"].notna()
    component["observed_weight"] = component["weight"].where(
        component["observed_margin"], 0.0
    )
    daily_weight = component.groupby("dt", observed=True).agg(
        total_weight=("weight", "sum"), observed_weight=("observed_weight", "sum")
    )
    daily_weight["coverage"] = daily_weight["observed_weight"] / daily_weight["total_weight"]
    etf_calendar = pd.DatetimeIndex(sorted(membership["dt"].unique()))
    etf = margin.loc[margin["ts_code"].eq(protocol["trade_symbol"])].set_index("trade_date")
    etf_coverage = float(etf_calendar.isin(etf.index).mean())
    observed_values = pd.concat(
        [
            component.loc[component["observed_margin"], VALUE_COLUMNS],
            etf[VALUE_COLUMNS],
        ],
        ignore_index=True,
    )
    finite = bool(np.isfinite(observed_values.to_numpy(dtype=float)).all())
    nonnegative = bool(observed_values.ge(0).all().all())
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "first_margin_date": margin["trade_date"].min().date().isoformat(),
        "last_margin_date": margin["trade_date"].max().date().isoformat(),
        "symbols_requested": len(codes),
        "symbols_observed": int(margin["ts_code"].nunique()),
        "api_calls": len(codes),
        "margin_rows": int(len(margin)),
        "duplicate_security_date_rows": duplicates,
        "etf_session_coverage": etf_coverage,
        "component_weight_coverage": float(component["observed_weight"].sum() / component["weight"].sum()),
        "component_daily_weight_coverage_p10": float(daily_weight["coverage"].quantile(0.1)),
        "finite_observed_values": finite,
        "nonnegative_observed_values": nonnegative,
        "causal_membership": bool((component["snapshot_date"] < component["dt"]).all()),
    }
    quality["passed"] = bool(
        quality["etf_session_coverage"] >= gate["minimum_etf_session_coverage"]
        and quality["component_weight_coverage"] >= gate["minimum_component_weight_coverage"]
        and quality["component_daily_weight_coverage_p10"]
        >= gate["minimum_component_daily_weight_coverage_p10"]
        and duplicates == 0
        and finite
        and nonnegative
        and quality["causal_membership"]
        and len(codes) <= gate["maximum_backfill_calls"]
    )

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    margin_out = margin.copy()
    margin_out["trade_date"] = margin_out["trade_date"].dt.strftime("%Y-%m-%d")
    margin_out.to_csv(
        artifacts / "margin_detail.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    component_out = component.copy()
    component_out["dt"] = component_out["dt"].dt.strftime("%Y-%m-%d")
    component_out["snapshot_date"] = component_out["snapshot_date"].dt.strftime("%Y-%m-%d")
    component_out.to_csv(
        artifacts / "component_margin_panel.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    (artifacts / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S005 EX22 执行\n\n"
        f"数据门结果：`{status}`。请求{quality['symbols_requested']}只证券、观测"
        f"{quality['symbols_observed']}只、{quality['margin_rows']}条明细。ETF交易日覆盖"
        f"{quality['etf_session_coverage']:.2%}，成分权重总体覆盖"
        f"{quality['component_weight_coverage']:.2%}，逐日覆盖P10为"
        f"{quality['component_daily_weight_coverage_p10']:.2%}。本轮未计算信号或收益。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_MARGIN_MECHANISM_PREREGISTRATION" if quality["passed"] else "STOP_MARGIN_ROUTE_ON_DATA"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX22 结论\n\n"
        f"裁决：`{decision}`。没有创建候选、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": protocol["trade_symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "status": status,
            "reads_post_event_prices": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
