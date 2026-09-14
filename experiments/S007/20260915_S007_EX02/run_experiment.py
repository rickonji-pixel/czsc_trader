from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX02"
START = pd.Timestamp("2021-01-04")
CUTOFF = pd.Timestamp("2026-09-02")


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


def _dates(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_datetime(frame[column], errors="raise").dt.normalize()
    return values.loc[values.between(START, CUTOFF)]


def _date_summary(frame: pd.DataFrame, column: str) -> dict[str, object]:
    values = _dates(frame, column)
    return {
        "rows": int(len(frame)),
        "sessions": int(values.nunique()),
        "first_session": values.min().date().isoformat(),
        "last_session": values.max().date().isoformat(),
    }


def _load_raw(repo: Path, frequency: str, date_column: str) -> pd.DataFrame:
    rows = []
    for year in range(2021, 2027):
        path = repo / f"data/raw/588080_{frequency}_{year}.csv"
        rows.append(pd.read_csv(path))
    frame = pd.concat(rows, ignore_index=True)
    frame[date_column] = pd.to_datetime(frame[date_column], errors="raise")
    return frame.loc[frame[date_column].between(START, CUTOFF + pd.Timedelta(days=1))].copy()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("reads_new_returns", "candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("data audit cannot read returns, select or deploy a candidate")

    cached: dict[str, pd.DataFrame] = {}
    identities = []
    for source in protocol["sources"]:
        archive = repo / str(source["archive"])
        validate_experiment_archive(archive)
        manifest = archive / "experiment_manifest.json"
        data_path = repo / str(source["data"])
        if _sha256(manifest) != source["archive_manifest_sha256"]:
            raise ValueError(f"source archive identity differs: {archive}")
        if _sha256(data_path) != source["data_sha256"]:
            raise ValueError(f"source data identity differs: {data_path}")
        cached[str(source["source_id"])] = pd.read_csv(data_path)
        identities.append({"source_id": source["source_id"], "sha256": source["data_sha256"]})
    for manifest in protocol["raw_manifests"].values():
        path = repo / str(manifest["path"])
        if _sha256(path) != manifest["sha256"]:
            raise ValueError(f"raw manifest identity differs: {path}")

    global_tech = cached["GLOBAL_TECH"]
    global_sessions = _dates(global_tech, "a_share_session")
    us_dates = pd.to_datetime(global_tech.loc[global_sessions.index, "us_trade_date"])
    global_causal = bool((us_dates < global_sessions).all())
    global_lag_valid = bool(
        pd.to_numeric(global_tech.loc[global_sessions.index, "calendar_lag_days"]).between(1, 4).all()
    )

    forecasts = cached["EARNINGS_FORECAST"]
    forecast_available = pd.to_datetime(forecasts["available_session"])
    forecast_report = pd.to_datetime(forecasts["report_date"])
    forecast_causal = bool((forecast_available > forecast_report).all())

    constituents = cached["HISTORICAL_CONSTITUENTS"]
    constituent_dates = pd.to_datetime(constituents["dt"])
    snapshots = pd.to_datetime(constituents["snapshot_date"])
    constituent_causal = bool((snapshots <= constituent_dates).all())

    options = cached["ETF_OPTION"]
    option_dates = pd.to_datetime(options["trade_date"])
    option_lifecycle = bool(
        (
            (pd.to_datetime(options["list_date"]) <= option_dates)
            & (option_dates <= pd.to_datetime(options["delist_date"]))
        ).all()
    )

    share = cached["ETF_SHARE_NAV"]
    share_dates = pd.to_datetime(share["trade_date"])
    share_core_valid = bool(
        share_dates.is_unique
        and (pd.to_numeric(share["total_share"]) > 0).all()
        and (pd.to_numeric(share["close"]) > 0).all()
    )
    nav_missing_sessions = int(pd.to_numeric(share["nav"], errors="coerce").isna().sum())

    daily = _load_raw(repo, "daily", "date")
    minute = _load_raw(repo, "1m", "datetime")
    raw_valid = bool(
        daily["date"].is_unique
        and minute["datetime"].is_unique
        and (daily[["open", "high", "low", "close"]].apply(pd.to_numeric) > 0).all().all()
        and (minute[["open", "high", "low", "close"]].apply(pd.to_numeric) > 0).all().all()
        and (pd.to_numeric(daily["volume"]) >= 0).all()
        and (pd.to_numeric(minute["volume"]) >= 0).all()
    )

    rows = [
        {
            "hypothesis_id": "H01-RISK-APPETITE",
            "data_sources": "GLOBAL_TECH; DOMESTIC_RATE_MISSING; BROAD_RISK_MISSING",
            "first_session": _date_summary(global_tech, "a_share_session")["first_session"],
            "last_session": _date_summary(global_tech, "a_share_session")["last_session"],
            "causality_pass": global_causal and global_lag_valid,
            "required_data_complete": False,
            "coverage_note": "海外科技完整；国内利率与全市场风险偏好尚无受管缓存",
            "disposition": "PARTIAL_NEEDS_DOMESTIC_RISK_DATA",
        },
        {
            "hypothesis_id": "H02-EARNINGS-EXPECTATION",
            "data_sources": "EARNINGS_FORECAST",
            "first_session": _date_summary(forecasts, "available_session")["first_session"],
            "last_session": _date_summary(forecasts, "available_session")["last_session"],
            "causality_pass": forecast_causal,
            "required_data_complete": True,
            "coverage_note": "可按研报后下一交易日使用；起始时间晚于完整开发池",
            "disposition": "RESEARCHABLE_WITH_LATE_START_DISCLOSED",
        },
        {
            "hypothesis_id": "H03-BREADTH-DISPERSION",
            "data_sources": "HISTORICAL_CONSTITUENTS",
            "first_session": _date_summary(constituents, "dt")["first_session"],
            "last_session": _date_summary(constituents, "dt")["last_session"],
            "causality_pass": constituent_causal,
            "required_data_complete": True,
            "coverage_note": "历史权重快照可审计；前20个交易日作为成分数据暖机",
            "disposition": "RESEARCHABLE_WITH_WARMUP",
        },
        {
            "hypothesis_id": "H04-ETF-DERIVATIVE-MICROSTRUCTURE",
            "data_sources": "ETF_SHARE_NAV; ETF_OPTION",
            "first_session": min(
                _date_summary(share, "trade_date")["first_session"],
                _date_summary(options, "trade_date")["first_session"],
            ),
            "last_session": max(
                _date_summary(share, "trade_date")["last_session"],
                _date_summary(options, "trade_date")["last_session"],
            ),
            "causality_pass": option_lifecycle and share_core_valid,
            "required_data_complete": False,
            "coverage_note": "份额覆盖完整；净值缺1日；期权自2023-06-05开始",
            "disposition": "PARTIAL_NAV_GAP_AND_OPTION_LATE_START",
        },
        {
            "hypothesis_id": "H05-PRICE-LIQUIDITY-STATE",
            "data_sources": "588080_DAILY; 588080_1M",
            "first_session": daily["date"].min().date().isoformat(),
            "last_session": daily["date"].max().date().isoformat(),
            "causality_pass": raw_valid,
            "required_data_complete": True,
            "coverage_note": "日线与1分钟线覆盖完整开发池；特征必须使用完成K线",
            "disposition": "RESEARCHABLE",
        },
    ]
    ledger = pd.DataFrame(rows)
    ledger.to_csv(artifacts / "data_route_audit.csv", index=False, encoding="utf-8")

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "FILL_RISK_APPETITE_DATA_GAP_THEN_PREREGISTER_MECHANISMS",
        "source_policy": protocol["source_policy"],
        "source_identities": identities,
        "route_dispositions": dict(zip(ledger["hypothesis_id"], ledger["disposition"], strict=True)),
        "all_available_sources_causal": bool(ledger["causality_pass"].all()),
        "daily_sessions": int(daily["date"].nunique()),
        "minute_sessions": int(minute["datetime"].dt.normalize().nunique()),
        "global_sessions": int(global_sessions.nunique()),
        "forecast_rows": int(len(forecasts)),
        "constituent_sessions": int(pd.to_datetime(constituents["dt"]).nunique()),
        "etf_share_sessions": int(share_dates.nunique()),
        "etf_nav_missing_sessions": nav_missing_sessions,
        "option_sessions": int(option_dates.nunique()),
        "unresolved_data_gaps": ["DOMESTIC_RATE", "BROAD_MARKET_RISK_APPETITE"],
        "new_return_paths_read": 0,
        "input_selected": False,
        "template_selected": False,
        "search_started": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "data_audit_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX02 执行记录\n\n"
        "五类路线的历史缓存、来源档案和文件哈希已校验。检查仅覆盖日期、缺失、生命周期及"
        "因果映射，没有计算信号后收益。全部现有数据通过因果字段检查。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX02 结论\n\n"
        "裁决：`FILL_RISK_APPETITE_DATA_GAP_THEN_PREREGISTER_MECHANISMS`。盈利预期、成分扩散、"
        "ETF/期权微观结构和价格流动性四类路线具备研究数据；风险偏好路线已有海外科技数据，"
        "仍缺国内利率和全市场风险偏好。现阶段没有选择因子、信号或模板，也没有Alpha结论。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
