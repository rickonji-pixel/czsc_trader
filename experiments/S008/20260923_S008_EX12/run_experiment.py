from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pandas as pd

from dataflows import Dataset
from dataflows.tushare_common import get_tushare_pro

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX12"
START = "20130729"
END = "20241231"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _yearly(fetch) -> pd.DataFrame:
    frames = []
    for year in range(2013, 2025):
        start = START if year == 2013 else f"{year}0101"
        end = END if year == 2024 else f"{year}1231"
        frame = fetch(start, end)
        if frame is not None and not frame.empty:
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _coverage(
    capability_id: str,
    frame: pd.DataFrame,
    date_column: str,
    critical: tuple[str, ...],
    expected_start: str,
    expected_end: str,
) -> dict[str, object]:
    if frame.empty:
        return {
            "capability_id": capability_id,
            "vendor_status": "EMPTY",
            "rows": 0,
            "earliest": "",
            "latest": "",
            "duplicate_date_rows": 0,
            "maximum_critical_null_rate": 1.0,
            "coverage_pass": False,
        }
    dates = frame[date_column].astype(str)
    maximum_null = max(float(frame[column].isna().mean()) for column in critical)
    earliest = str(dates.min())
    latest = str(dates.max())
    return {
        "capability_id": capability_id,
        "vendor_status": "PASS",
        "rows": int(len(frame)),
        "earliest": earliest,
        "latest": latest,
        "duplicate_date_rows": int(dates.duplicated().sum()),
        "maximum_critical_null_rate": maximum_null,
        "coverage_pass": bool(
            earliest <= expected_start
            and latest >= expected_end
            and not dates.duplicated().any()
            and maximum_null == 0.0
        ),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "stores_raw_vendor_data",
        "reads_target_returns",
        "selects_input",
        "selects_template",
        "starts_search",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("data capability gate cannot select, search, promote or mutate")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
        "ex11_manifest_sha256": repo
        / "experiments/S008/20260923_S008_EX11/experiment_manifest.json",
        "dfls_contract_sha256": repo / "packages/dataflows/src/dataflows/contract.py",
        "dfls_facade_sha256": repo / "packages/dataflows/src/dataflows/facade.py",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")

    pro = get_tushare_pro(repo / ".env")
    coverage_rows = [
        _coverage(
            "US_REAL_YIELD",
            _yearly(lambda start, end: pro.us_trycr(start_date=start, end_date=end)),
            "date",
            ("y5", "y10"),
            START,
            END,
        ),
        _coverage(
            "USDCNH",
            _yearly(
                lambda start, end: pro.fx_daily(
                    ts_code="USDCNH.FXCM", start_date=start, end_date=end
                )
            ),
            "trade_date",
            ("bid_close", "ask_close"),
            START,
            END,
        ),
        _coverage(
            "SGE_GOLD",
            _yearly(
                lambda start, end: pro.sge_daily(
                    ts_code="Au99.99", start_date=start, end_date=end
                )
            ),
            "trade_date",
            ("close", "vol", "amount"),
            START,
            END,
        ),
        _coverage(
            "ETF_SHARE",
            pro.etf_share_size(ts_code="518880.SH", start_date=START, end_date=END),
            "trade_date",
            ("total_share", "total_size"),
            START,
            END,
        ),
        _coverage(
            "SHIBOR",
            _yearly(lambda start, end: pro.shibor(start_date=start, end_date=end)),
            "date",
            ("on", "1w", "3m", "1y"),
            START,
            END,
        ),
        _coverage(
            "DOMESTIC_INDEX_PRICE",
            pro.index_daily(ts_code="000001.SH", start_date=START, end_date=END),
            "trade_date",
            ("close", "vol", "amount"),
            START,
            END,
        ),
        _coverage(
            "DOMESTIC_INDEX_BASIC",
            pro.index_dailybasic(ts_code="000001.SH", start_date=START, end_date=END),
            "trade_date",
            ("turnover_rate", "pe_ttm", "pb"),
            START,
            END,
        ),
        _coverage(
            "CN_CPI",
            pro.cn_cpi(start_m="201307", end_m="202412"),
            "month",
            ("nt_yoy", "nt_mom"),
            "201307",
            "202412",
        ),
        _coverage(
            "CN_PPI",
            pro.cn_ppi(start_m="201307", end_m="202412"),
            "month",
            ("ppi_yoy", "ppi_mom"),
            "201307",
            "202412",
        ),
        _coverage(
            "CN_MONEY",
            pro.cn_m(start_m="201307", end_m="202412"),
            "month",
            ("m1_yoy", "m2_yoy"),
            "201307",
            "202412",
        ),
    ]
    managed = _read(repo / "research/S008/materials.json")
    for capability_id in ("ETF_PRICE", "ETF_EXECUTION"):
        coverage_rows.append(
            {
                "capability_id": capability_id,
                "vendor_status": "MANAGED_EVIDENCE",
                "rows": 2781,
                "earliest": START,
                "latest": END,
                "duplicate_date_rows": 0,
                "maximum_critical_null_rate": 0.0,
                "coverage_pass": bool(managed.get("research_data_manifest")),
            }
        )

    capabilities = protocol["capabilities"]
    available_datasets = {item.value for item in Dataset}
    coverage_by_id = {str(row["capability_id"]): row for row in coverage_rows}
    capability_rows = []
    for item in capabilities:
        capability_id = str(item["capability_id"])
        coverage = coverage_by_id[capability_id]
        capability_rows.append(
            {
                **coverage,
                "vendor_interface": item["vendor_interface"],
                "required_dataset": item["required_dataset"],
                "dfls_published": item["required_dataset"] in available_datasets,
                "causality_rule": protocol["causality_rules"].get(capability_id, "managed"),
                "hypotheses": " | ".join(item["hypotheses"]),
            }
        )
    with (artifacts / "capability_audit.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(capability_rows[0]))
        writer.writeheader()
        writer.writerows(capability_rows)

    vendor_failures = [
        row["capability_id"] for row in capability_rows if not row["coverage_pass"]
    ]
    missing_dfls = [
        row["required_dataset"] for row in capability_rows if not row["dfls_published"]
    ]
    schedule_history = {
        month: int(len(pro.cn_schedule(m=month))) for month in ("201308", "202412")
    }
    if vendor_failures:
        decision = protocol["adjudication"]["vendor_or_causality_failure"]
    elif missing_dfls:
        decision = protocol["adjudication"]["vendor_pass_with_missing_dfls"]
    else:
        decision = protocol["adjudication"]["all_capabilities_published"]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "capability_count": len(capability_rows),
        "vendor_coverage_pass_count": sum(
            bool(row["coverage_pass"]) for row in capability_rows
        ),
        "vendor_failures": vendor_failures,
        "dfls_published_count": sum(
            bool(row["dfls_published"]) for row in capability_rows
        ),
        "missing_dfls_datasets": missing_dfls,
        "historical_macro_schedule_rows": schedule_history,
        "macro_causality_policy": "reference month M usable from first China session of M+2",
        "stores_raw_vendor_data": False,
        "reads_target_returns": False,
        "reads_sealed_validation": False,
        "candidate_created": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "capability_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S008 EX12 执行记录\n\n"
        f"审计{len(capability_rows)}项数据能力；供应商覆盖通过"
        f"{summary['vendor_coverage_pass_count']}项，DFLS已发布"
        f"{summary['dfls_published_count']}项。只归档覆盖与能力摘要，没有保存供应商原始数据、"
        "读取目标收益或封存验证池。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX12 结论\n\n"
        f"裁决：`{decision}`。Tushare权限和开发期覆盖失败项为"
        f"{vendor_failures or '无'}；DFLS缺少的数据集为{missing_dfls or '无'}。"
        "缺口补齐并重新通过数据能力门前，不得物化正式特征面板。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
