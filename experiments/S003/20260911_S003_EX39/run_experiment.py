from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_execution_prices
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX39"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _record_failure(
    experiment: Path,
    artifacts: Path,
    dataset: dict[str, object],
    reason: str,
    detail: str,
) -> None:
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "passed": False,
        "failure_reason": reason,
        "failure_detail": detail,
        "conditional_return_analysis": False,
    }
    _write_json(artifacts / "data_quality.json", quality)
    (experiment / "03_execution.md").write_text(
        "# S003 EX39 执行\n\n"
        f"数据门结果：`FAIL`。失败原因：`{reason}`。没有读取任何条件收益。供应商或数据"
        "质量明细保存在机器证据中。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX39 结论\n\n"
        "结论：`FAIL`。5000积分接口组合未满足ETF份额与净值研究的数据门。"
        "本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": "FAIL",
            "failure_reason": reason,
            "conditional_return_analysis": False,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


def _first_session_after(calendar: pd.DatetimeIndex, value: pd.Timestamp) -> pd.Timestamp:
    later = calendar[calendar > value]
    return later.min() if len(later) else pd.NaT


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "conditional_return_analysis",
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX39 may only validate legacy ETF share and NAV data")

    dataset = protocol["dataset"]
    execution_manifest = repo_root / dataset["execution_manifest"]
    if _sha256(execution_manifest) != dataset["execution_manifest_sha256"]:
        raise ValueError("510500 execution manifest differs from frozen evidence")
    prices = load_execution_prices(
        repo_root / "data" / "raw",
        protocol["research_target"]["trade_symbol"],
        asset_type="etf",
        cutoff=dataset["development_cutoff"],
    )
    prices = prices.loc[prices["dt"] >= pd.Timestamp(dataset["start"])].copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    calendar = pd.DatetimeIndex(prices["dt"])
    if calendar.has_duplicates or not calendar.is_monotonic_increasing:
        raise ValueError("510500 execution calendar must be unique and ascending")

    sources = protocol["sources"]
    try:
        pro = get_tushare_pro(repo_root / ".env")
        share = pro.fund_share(
            ts_code=protocol["research_target"]["trade_symbol"],
            start_date=dataset["start"].replace("-", ""),
            end_date=dataset["development_cutoff"].replace("-", ""),
            fields=",".join(sources["share_fields"]),
        )
        nav = pro.fund_nav(
            ts_code=protocol["research_target"]["trade_symbol"],
            start_date=dataset["start"].replace("-", ""),
            end_date=dataset["development_cutoff"].replace("-", ""),
            market="E",
            fields=",".join(sources["nav_fields"]),
        )
    except Exception as exc:  # Tushare exposes provider failures as a generic exception.
        _record_failure(experiment, artifacts, dataset, "PROVIDER_ACCESS_UNAVAILABLE", str(exc))
        return
    if share is None or share.empty or nav is None or nav.empty:
        detail = f"share_rows={0 if share is None else len(share)},nav_rows={0 if nav is None else len(nav)}"
        _record_failure(experiment, artifacts, dataset, "EMPTY_PROVIDER_DATA", detail)
        return

    share = share.copy()
    nav = nav.copy()
    share_missing = sorted(set(sources["share_fields"]).difference(share.columns))
    nav_missing = sorted(set(sources["nav_fields"]).difference(nav.columns))
    if share_missing or nav_missing:
        _record_failure(
            experiment,
            artifacts,
            dataset,
            "MISSING_PROVIDER_FIELDS",
            f"share={share_missing},nav={nav_missing}",
        )
        return
    share["dt"] = pd.to_datetime(share["trade_date"].astype(str), format="%Y%m%d").dt.normalize()
    nav["dt"] = pd.to_datetime(nav["nav_date"].astype(str), format="%Y%m%d").dt.normalize()
    nav["announcement_date"] = pd.to_datetime(
        nav["ann_date"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.normalize()
    share = share.loc[share["dt"].between(calendar.min(), calendar.max())].sort_values("dt")
    nav = nav.loc[nav["dt"].between(calendar.min(), calendar.max())].sort_values("dt")
    share["fd_share"] = pd.to_numeric(share["fd_share"], errors="coerce")
    nav["unit_nav"] = pd.to_numeric(nav["unit_nav"], errors="coerce")

    share_dates = pd.DatetimeIndex(share["dt"])
    nav_dates = pd.DatetimeIndex(nav["dt"])
    share_duplicate_rows = int(share.duplicated("dt", keep=False).sum())
    nav_duplicate_rows = int(nav.duplicated("dt", keep=False).sum())
    share_coverage = float(len(calendar.intersection(share_dates.unique())) / len(calendar))
    nav_coverage = float(len(calendar.intersection(nav_dates.unique())) / len(calendar))
    positive_finite = bool(
        np.isfinite(share["fd_share"].to_numpy(dtype=float)).all()
        and (share["fd_share"] > 0).all()
        and np.isfinite(nav["unit_nav"].to_numpy(dtype=float)).all()
        and (nav["unit_nav"] > 0).all()
    )
    announcement_valid = bool(
        nav["announcement_date"].notna().all()
        and nav["announcement_date"].ge(nav["dt"]).all()
    )

    share["share_available_date"] = share["dt"].map(lambda value: _first_session_after(calendar, value))
    nav["nav_available_date"] = nav["announcement_date"].map(
        lambda value: _first_session_after(calendar, value) if pd.notna(value) else pd.NaT
    )
    panel = prices[["dt", "close"]].merge(
        share[["dt", "fd_share", "share_available_date"]], on="dt", how="inner", validate="one_to_one"
    )
    panel = panel.merge(
        nav[["dt", "announcement_date", "unit_nav", "nav_available_date"]],
        on="dt",
        how="inner",
        validate="one_to_one",
    )
    panel["combined_available_date"] = panel[["share_available_date", "nav_available_date"]].max(
        axis=1
    )
    positions = pd.Series(range(len(calendar)), index=calendar)
    panel["combined_availability_delay_sessions"] = [
        (
            float(positions.loc[available] - positions.loc[date])
            if pd.notna(available) and available in positions.index
            else np.nan
        )
        for date, available in zip(panel["dt"], panel["combined_available_date"], strict=True)
    ]
    panel["close_nav_deviation"] = panel["close"] / panel["unit_nav"] - 1.0
    observable_delays = panel["combined_availability_delay_sessions"].dropna()
    p95_delay = float(observable_delays.quantile(0.95)) if len(observable_delays) else float("inf")
    max_close_nav_deviation = float(panel["close_nav_deviation"].abs().max())

    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "access_available": True,
        "master_sessions": int(len(calendar)),
        "share_rows": int(len(share)),
        "nav_rows": int(len(nav)),
        "combined_rows": int(len(panel)),
        "first_master_session": calendar.min().strftime("%Y-%m-%d"),
        "last_master_session": calendar.max().strftime("%Y-%m-%d"),
        "first_share_session": share["dt"].min().strftime("%Y-%m-%d"),
        "last_share_session": share["dt"].max().strftime("%Y-%m-%d"),
        "first_nav_session": nav["dt"].min().strftime("%Y-%m-%d"),
        "last_nav_session": nav["dt"].max().strftime("%Y-%m-%d"),
        "share_calendar_coverage_ratio": share_coverage,
        "nav_calendar_coverage_ratio": nav_coverage,
        "share_duplicate_date_rows": share_duplicate_rows,
        "nav_duplicate_date_rows": nav_duplicate_rows,
        "positive_finite_share_and_nav": positive_finite,
        "announcement_not_before_nav_date": announcement_valid,
        "p95_combined_availability_delay_sessions": p95_delay,
        "max_absolute_close_nav_deviation": max_close_nav_deviation,
        "share_missing_calendar_dates": [
            value.strftime("%Y-%m-%d") for value in calendar.difference(share_dates)
        ],
        "nav_missing_calendar_dates": [
            value.strftime("%Y-%m-%d") for value in calendar.difference(nav_dates)
        ],
        "forward_filled_rows": 0,
        "share_publication_time_risk": "NO_ANNOUNCEMENT_TIMESTAMP_USE_NEXT_SESSION",
    }
    quality["passed"] = bool(
        share_duplicate_rows == 0
        and nav_duplicate_rows == 0
        and calendar.min() in share_dates
        and calendar.max() in share_dates
        and calendar.min() in nav_dates
        and calendar.max() in nav_dates
        and share_coverage >= float(gate["minimum_share_calendar_coverage_ratio"])
        and nav_coverage >= float(gate["minimum_nav_calendar_coverage_ratio"])
        and positive_finite
        and announcement_valid
        and p95_delay <= float(gate["maximum_p95_combined_availability_delay_sessions"])
        and max_close_nav_deviation <= float(gate["maximum_absolute_close_nav_deviation"])
    )

    output = panel.copy()
    for column in ("dt", "announcement_date", "share_available_date", "nav_available_date", "combined_available_date"):
        output[column] = pd.to_datetime(output[column]).dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "etf_share_nav_panel.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    _write_json(artifacts / "data_quality.json", quality)
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX39 执行\n\n"
        f"数据门结果：`{status}`。主日历{quality['master_sessions']}日，份额"
        f"{quality['share_rows']}日（{share_coverage:.2%}），净值{quality['nav_rows']}日"
        f"（{nav_coverage:.2%}），组合面板{quality['combined_rows']}日。组合可用延迟P95为"
        f"{p95_delay:.1f}个交易日，收盘相对净值最大绝对偏离{max_close_nav_deviation:.2%}。\n\n"
        "本轮没有读取条件收益、生成信号或选择阈值。\n",
        encoding="utf-8",
    )
    conclusion = (
        "5000积分接口组合满足下一轮ETF资金流机制研究的数据要求。"
        if quality["passed"]
        else "5000积分接口组合未满足冻结的数据门，当前不得进入收益研究。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX39 结论\n\n"
        f"结论：`{status}`。{conclusion}份额无公告时间字段的风险继续保留。"
        "本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "conditional_return_analysis": False,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
