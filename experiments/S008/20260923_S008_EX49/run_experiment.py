from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from dataflows import DataRequest, DataStatus, Dataflows
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX49"


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


def _fetch(dataflows: Dataflows, specification: dict[str, object], repo: Path):
    result = dataflows.fetch(
        DataRequest(
            dataset=str(specification["dataset"]),
            symbol=str(specification["symbol"]),
            start=str(specification["start"]),
            end=str(specification["cutoff"]),
            required_cutoff=str(specification["cutoff"]),
            frequency=str(specification.get("frequency", "daily")),
            options={"env_file": repo / ".env"},
        )
    )
    if result.status is not DataStatus.READY:
        code = result.error.code if result.error else "UNKNOWN"
        raise RuntimeError(f"DFLS {specification['dataset']} is {result.status.value}: {code}")
    return result


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "stores_raw_vendor_data",
        "selects_strategy",
        "starts_search",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("proxy gate cannot save raw data, select, search, promote or mutate")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
        "ex48_manifest_sha256": repo / "experiments/S008/20260923_S008_EX48/experiment_manifest.json",
        "dfls_contract_sha256": repo / "packages/dataflows/src/dataflows/contract.py",
        "dfls_facade_sha256": repo / "packages/dataflows/src/dataflows/facade.py",
        "dfls_adapter_sha256": repo / "packages/dataflows/src/dataflows/tushare_strategy_data.py",
        "target_manifest_sha256": repo / "data/raw/518880_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")

    dataflows = Dataflows()
    proxy_spec = dict(protocol["proxy"])
    target_spec = dict(protocol["target"])
    proxy_result = _fetch(dataflows, proxy_spec, repo)
    target_result = _fetch(dataflows, target_spec, repo)
    proxy = proxy_result.dataframe.sort_values("Date").reset_index(drop=True)
    target = target_result.dataframe.sort_values("Date").reset_index(drop=True)

    required_fields = [str(value) for value in proxy_spec["required_fields"]]
    missing_fields = sorted(set(required_fields).difference(proxy.columns))
    proxy_dates = pd.to_datetime(proxy["Date"])
    proxy_close = pd.to_numeric(proxy["Close"], errors="coerce")
    data_checks = {
        "status_ready": True,
        "exact_start": proxy_dates.min().date().isoformat() == proxy_spec["start"],
        "exact_cutoff": proxy_dates.max().date().isoformat() == proxy_spec["cutoff"],
        "minimum_rows": len(proxy) >= int(proxy_spec["minimum_rows"]),
        "no_duplicate_dates": not proxy_dates.duplicated().any(),
        "required_fields_present": not missing_fields,
        "required_fields_complete": not missing_fields and all(proxy[field].notna().all() for field in required_fields),
        "positive_close": bool((proxy_close > 0).all()),
        "tushare_identity": proxy_result.identity is not None and proxy_result.identity.source == "tushare",
    }
    data_gate_pass = all(data_checks.values())

    proxy_returns = pd.DataFrame({"Date": proxy_dates, "proxy_return_1d": proxy_close.pct_change(), "proxy_return_20d": proxy_close.pct_change(20)})
    target_dates = pd.to_datetime(target["Date"])
    target_close = pd.to_numeric(target["Close"], errors="coerce")
    target_returns = pd.DataFrame({"Date": target_dates, "target_return_1d": target_close.pct_change(), "target_return_20d": target_close.pct_change(20)})
    overlap = proxy_returns.merge(target_returns, on="Date", how="inner")
    daily = overlap.dropna(subset=["proxy_return_1d", "target_return_1d"])
    returns_20d = overlap.dropna(subset=["proxy_return_20d", "target_return_20d"])
    fidelity = {
        "overlap_daily_return_rows": int(len(daily)),
        "daily_pearson": float(daily["proxy_return_1d"].corr(daily["target_return_1d"], method="pearson")),
        "daily_spearman": float(daily["proxy_return_1d"].corr(daily["target_return_1d"], method="spearman")),
        "daily_direction_agreement": float((np.sign(daily["proxy_return_1d"]) == np.sign(daily["target_return_1d"])).mean()),
        "return_20d_rows": int(len(returns_20d)),
        "return_20d_pearson": float(returns_20d["proxy_return_20d"].corr(returns_20d["target_return_20d"], method="pearson")),
        "return_20d_spearman": float(returns_20d["proxy_return_20d"].corr(returns_20d["target_return_20d"], method="spearman")),
    }
    gates = protocol["gates"]
    fidelity_checks = {
        "minimum_overlap_daily_returns": fidelity["overlap_daily_return_rows"] >= int(gates["minimum_overlap_daily_returns"]),
        "minimum_daily_pearson": fidelity["daily_pearson"] >= float(gates["minimum_daily_pearson"]),
        "minimum_daily_spearman": fidelity["daily_spearman"] >= float(gates["minimum_daily_spearman"]),
        "minimum_return_20d_pearson": fidelity["return_20d_pearson"] >= float(gates["minimum_return_20d_pearson"]),
    }
    fidelity_gate_pass = all(fidelity_checks.values())

    annual_rows = []
    year_end = proxy.assign(Year=proxy_dates.dt.year, _close=proxy_close).groupby("Year", sort=True).tail(1).set_index("Year")["_close"]
    year_range = protocol["pre_etf_full_years"]
    regime = protocol["regime_definition"]
    for year in range(int(year_range["start"]), int(year_range["end"]) + 1):
        year_frame = proxy.loc[proxy_dates.dt.year.eq(year)].copy()
        closes = pd.to_numeric(year_frame["Close"], errors="coerce")
        annual_return = float(year_end.loc[year] / year_end.loc[year - 1] - 1.0)
        maximum_drawdown = float((closes / closes.cummax() - 1.0).min())
        expansion = annual_return >= float(regime["expansion_min_return"])
        contraction = annual_return <= float(regime["contraction_max_return"])
        shock = maximum_drawdown <= float(regime["shock_max_drawdown"])
        annual_rows.append({
            "year": year,
            "sessions": int(len(year_frame)),
            "annual_return": annual_return,
            "maximum_drawdown": maximum_drawdown,
            "expansion": expansion,
            "contraction": contraction,
            "shock": shock,
        })
    complete_years = sum(row["sessions"] >= 230 for row in annual_rows)
    expansion_years = sum(bool(row["expansion"]) for row in annual_rows)
    contraction_years = sum(bool(row["contraction"]) for row in annual_rows)
    shock_years = sum(bool(row["shock"]) for row in annual_rows)
    sample_checks = {
        "minimum_complete_pre_etf_years": complete_years >= int(gates["minimum_complete_pre_etf_years"]),
        "minimum_expansion_years": expansion_years >= int(gates["minimum_expansion_years"]),
        "minimum_contraction_or_shock_years": (contraction_years + shock_years) >= int(gates["minimum_contraction_or_shock_years"]),
    }
    sample_gate_pass = all(sample_checks.values())

    if not data_gate_pass:
        decision = protocol["adjudication"]["data_failure"]
    elif not fidelity_gate_pass:
        decision = protocol["adjudication"]["fidelity_failure"]
    elif not sample_gate_pass:
        decision = protocol["adjudication"]["sample_failure"]
    else:
        decision = protocol["adjudication"]["all_pass"]

    with (artifacts / "pre_etf_annual_regime_ledger.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(annual_rows[0]))
        writer.writeheader()
        writer.writerows(annual_rows)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "predecessor_experiment_id": protocol["predecessor_experiment_id"],
        "decision": decision,
        "proxy_identity": {
            "dataset": proxy_result.identity.dataset,
            "source": proxy_result.identity.source,
            "data_start": proxy_result.identity.data_start,
            "data_cutoff": proxy_result.identity.data_cutoff,
            "content_sha256": proxy_result.identity.content_sha256,
            "rows": int(len(proxy)),
        },
        "target_identity": {
            "dataset": target_result.identity.dataset,
            "source": target_result.identity.source,
            "data_start": target_result.identity.data_start,
            "data_cutoff": target_result.identity.data_cutoff,
            "content_sha256": target_result.identity.content_sha256,
            "rows": int(len(target)),
        },
        "data_checks": data_checks,
        "data_gate_pass": data_gate_pass,
        "fidelity": fidelity,
        "fidelity_checks": fidelity_checks,
        "fidelity_gate_pass": fidelity_gate_pass,
        "pre_etf_sample": {
            "complete_years": complete_years,
            "expansion_years": expansion_years,
            "contraction_years": contraction_years,
            "shock_years": shock_years,
        },
        "sample_checks": sample_checks,
        "sample_gate_pass": sample_gate_pass,
        "stores_raw_vendor_data": False,
        "reads_target_development_returns": True,
        "reads_sealed_validation": False,
        "candidate_created": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "proxy_audit_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S008 EX49 执行记录\n\n"
        f"DFLS交付Au99.99={len(proxy)}行、518880.SH开发池={len(target)}行。"
        f"重叠日收益={fidelity['overlap_daily_return_rows']}行；上市前完整年度={complete_years}。"
        "只归档身份、检查摘要和年度状态账本，没有保存供应商原始数据、读取密封验证池、"
        "启动搜索或形成候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX49 结论\n\n"
        f"机器裁决：`{decision}`。数据门={data_gate_pass}，代理一致性门={fidelity_gate_pass}，"
        f"样本增量门={sample_gate_pass}。上市前上涨年度={expansion_years}，收缩年度="
        f"{contraction_years}，冲击年度={shock_years}。本结果只决定是否进入上市前状态机制"
        "审计；年度桶不宣称统计独立，也不构成策略或候选证据。\n",
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
            "development_cutoff": target_spec["cutoff"],
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
