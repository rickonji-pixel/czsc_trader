from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX14"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper module: {path}")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def _materialize(
    repo: Path,
    protocol: dict[str, object],
    panel: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    helpers,
) -> dict[str, tuple[str, pd.Series]]:
    sources = protocol["sources"]
    wanted = set(panel["component_id"].astype(str))
    categorical_ids = set(panel.loc[panel["kind"].eq("CATEGORICAL_SIGNAL"), "component_id"].astype(str))
    continuous_ids = wanted - categorical_ids
    materialized: dict[str, tuple[str, pd.Series]] = {}

    primary_path = repo / str(sources["primary_states_path"])
    columns = pd.read_csv(primary_path, nrows=0).columns.tolist()
    primary_ids = sorted(categorical_ids.intersection(columns))
    if primary_ids:
        primary = pd.read_csv(primary_path, usecols=["dt", *primary_ids])
        primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
        primary = primary.set_index("dt").reindex(calendar)
        for component_id in primary_ids:
            materialized[component_id] = ("CATEGORICAL_SIGNAL", helpers._shift_after_close(primary[component_id], calendar))

    project_ids = categorical_ids - set(primary_ids)
    if project_ids:
        project = pd.read_csv(repo / str(sources["project_states_path"]), parse_dates=["first_usable_date"])
        project = project.loc[project["signal_id"].astype(str).isin(project_ids)]
        for signal_id, group in project.groupby("signal_id", sort=True):
            raw = helpers._map_first_usable(group, calendar, "state")
            raw.loc[raw.astype(str).eq("warmup")] = np.nan
            materialized[str(signal_id)] = ("CATEGORICAL_SIGNAL", raw)

    if continuous_ids:
        factor = pd.read_csv(repo / str(sources["factor_ledger_path"]))
        factor = factor.loc[factor["factor_id"].astype(str).isin(continuous_ids) & factor["value_numeric"].notna()]
        for factor_id, group in factor.groupby("factor_id", sort=True):
            materialized[str(factor_id)] = (
                "CONTINUOUS_FACTOR",
                helpers._map_first_usable(group, calendar, "value_numeric").astype(float),
            )
        if "F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW" in continuous_ids:
            industry = pd.read_csv(repo / str(sources["industry_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
            breadth = helpers._causal_percentile(industry["positive_member_ratio"], 252, 126)
            flow = helpers._causal_percentile(industry["net_flow_ratio"], 252, 126)
            value = helpers._causal_percentile(pd.concat([breadth, flow], axis=1).mean(axis=1, skipna=False), 252, 126)
            frame = pd.DataFrame({
                "first_usable_date": pd.Series(industry["trade_date"].to_numpy(), index=industry.index).map(
                    lambda item: helpers._next_calendar_session(calendar, pd.Timestamp(item))
                ),
                "first_usable_clock": "09:00:00",
                "value_numeric": value,
            }).dropna(subset=["first_usable_date"])
            materialized["F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW"] = (
                "CONTINUOUS_FACTOR",
                helpers._map_first_usable(frame, calendar, "value_numeric").astype(float),
            )
        if "F-PROJECT-SELL-SIDE-REVISION-BREADTH" in continuous_ids:
            analyst = pd.read_csv(repo / str(sources["analyst_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
            frame = pd.DataFrame({
                "first_usable_date": analyst["trade_date"],
                "first_usable_clock": "09:00:00",
                "value_numeric": analyst["revision_score"],
            })
            materialized["F-PROJECT-SELL-SIDE-REVISION-BREADTH"] = (
                "CONTINUOUS_FACTOR",
                helpers._map_first_usable(frame, calendar, "value_numeric").astype(float),
            )
    missing = wanted - set(materialized)
    if missing:
        raise ValueError(f"compressed components cannot be materialized: {sorted(missing)}")
    return materialized


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX14 protocol identity or return declaration differs")
    sources = protocol["sources"]
    compression = repo / "experiments/S006" / str(sources["compression_experiment_id"])
    validate_experiment_archive(compression)
    frozen = {
        compression / "experiment_manifest.json": sources["compression_manifest_sha256"],
        repo / str(sources["compressed_panel_path"]): sources["compressed_panel_sha256"],
        repo / str(sources["ex12_script_path"]): sources["ex12_script_sha256"],
        repo / str(sources["ex06_script_path"]): sources["ex06_script_sha256"],
        repo / str(sources["ex10_script_path"]): sources["ex10_script_sha256"],
        repo / str(sources["primary_states_path"]): sources["primary_states_sha256"],
        repo / str(sources["project_states_path"]): sources["project_states_sha256"],
        repo / str(sources["factor_ledger_path"]): sources["factor_ledger_sha256"],
        repo / str(sources["industry_path"]): sources["industry_sha256"],
        repo / str(sources["analyst_path"]): sources["analyst_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    h6 = _module(repo / str(sources["ex06_script_path"]), "s006_ex14_h6")
    h10 = _module(repo / str(sources["ex10_script_path"]), "s006_ex14_h10")
    h12 = _module(repo / str(sources["ex12_script_path"]), "s006_ex14_h12")
    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(
        context,
        str(protocol["dataset"]["name"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    if replay.fingerprint != str(protocol["dataset"]["fingerprint"]):
        raise ValueError("research dataset differs from protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    calendar = prices.index
    opens = prices["open"].astype(float)
    outcome = opens.shift(-1).div(opens).sub(1.0)
    dataset = protocol["dataset"]
    discovery = (calendar >= pd.Timestamp(dataset["discovery_start"])) & (calendar <= pd.Timestamp(dataset["discovery_end"]))
    confirmation = (calendar >= pd.Timestamp(dataset["confirmation_start"])) & (calendar <= pd.Timestamp(dataset["confirmation_end"]))

    panel = pd.read_csv(repo / str(sources["compressed_panel_path"]))
    architecture = protocol["architecture"]
    if len(panel) != int(architecture["expected_components"]) or panel["information_family"].nunique() != int(architecture["expected_information_families"]):
        raise ValueError("compressed panel dimensions differ from protocol")
    raw_components = _materialize(repo, protocol, panel, calendar, h6)
    apply_all = np.ones(len(calendar), dtype=bool)
    prior = float(architecture["categorical_shrinkage_prior_count"])
    component_scores: dict[str, pd.Series] = {}
    for component_id, row in panel.set_index("component_id").iterrows():
        kind, raw = raw_components[str(component_id)]
        if kind != str(row["kind"]):
            raise ValueError(f"component kind differs: {component_id}")
        component_scores[str(component_id)] = (
            h12._categorical_score(raw, outcome, discovery, apply_all, prior, h6)
            if kind == "CATEGORICAL_SIGNAL"
            else h12._continuous_score(raw, outcome, discovery, apply_all, h6)
        )
    scores = pd.DataFrame(component_scores, index=calendar)
    family_scores = {
        str(family): scores[group["component_id"].astype(str).tolist()].median(axis=1, skipna=True)
        for family, group in panel.groupby("information_family", sort=True)
    }
    families = pd.DataFrame(family_scores, index=calendar)
    train = pd.concat([families.loc[discovery], outcome.loc[discovery].rename("target")], axis=1).dropna()
    scaler = StandardScaler().fit(train[families.columns])
    ridge = Ridge(alpha=10.0, positive=True).fit(scaler.transform(train[families.columns]), train["target"])
    fill = families.loc[discovery].median()
    combined = pd.Series(ridge.predict(scaler.transform(families.fillna(fill))), index=calendar)
    quantile = float(architecture["entry_quantile"])
    threshold = float(combined.loc[discovery].quantile(quantile))
    positions = combined.ge(threshold).astype(float)
    fee = float(protocol["execution"]["fee_rate_one_way"])
    full = h10._metrics(positions, opens, fee)
    discovery_metrics = h10._metrics(positions.loc[discovery], opens.loc[discovery], fee)
    confirmation_metrics = h10._metrics(positions.loc[confirmation], opens.loc[confirmation], fee)
    gates = protocol["hard_gates"]
    checks = {
        "full_cagr": full["cagr"] >= float(gates["minimum_cagr"]),
        "full_maximum_drawdown": full["maximum_drawdown"] >= float(gates["maximum_drawdown_limit"]),
        "beats_s001_v2_drawdown": full["maximum_drawdown"] > float(gates["s001_v2_maximum_drawdown"]),
        "frequency_median": full["rolling_60_closed_trades_median"] >= float(gates["minimum_rolling_60_closed_trades_median"]),
        "frequency_p10": full["rolling_60_closed_trades_p10"] >= float(gates["minimum_rolling_60_closed_trades_p10"]),
        "confirmation_cagr": confirmation_metrics["cagr"] > float(gates["minimum_confirmation_cagr"]),
        "confirmation_maximum_drawdown": confirmation_metrics["maximum_drawdown"] >= float(gates["confirmation_maximum_drawdown_limit"]),
    }
    passed = all(checks.values())
    weights = pd.DataFrame({"information_family": families.columns, "ridge_weight": ridge.coef_})
    weights.to_csv(artifacts / "compressed_family_weights.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    row: dict[str, object] = {
        "path_id": "COMPRESSED21:H1:POSITIVE_RIDGE_A10:S1:Q50",
        "entry_threshold": threshold,
        "components": len(panel),
        "information_families": len(families.columns),
        "hard_gates_pass": passed,
        "gate_checks_json": json.dumps(checks, sort_keys=True),
    }
    for prefix, metrics in (("full", full), ("discovery", discovery_metrics), ("confirmation", confirmation_metrics)):
        row.update({f"{prefix}_{key}": value for key, value in metrics.items()})
    pd.DataFrame([row]).to_csv(artifacts / "compressed_replay_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    reference = protocol["reference"]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "components": len(panel),
        "information_families": len(families.columns),
        "nonzero_information_families": int((ridge.coef_ > 0).sum()),
        "full_cagr": float(full["cagr"]),
        "full_maximum_drawdown": float(full["maximum_drawdown"]),
        "full_calmar": float(full["calmar"]),
        "full_profit_factor": float(full["profit_factor"]),
        "full_closed_trades": int(full["closed_trades"]),
        "frequency_median": float(full["rolling_60_closed_trades_median"]),
        "frequency_p10": float(full["rolling_60_closed_trades_p10"]),
        "discovery_cagr": float(discovery_metrics["cagr"]),
        "confirmation_cagr": float(confirmation_metrics["cagr"]),
        "confirmation_maximum_drawdown": float(confirmation_metrics["maximum_drawdown"]),
        "cagr_retention_vs_ex12": float(full["cagr"] / float(reference["ex12_full_cagr"])),
        "drawdown_change_vs_ex12": float(full["maximum_drawdown"] - float(reference["ex12_full_maximum_drawdown"])),
        "hard_gate_checks": checks,
        "hard_gates_pass": passed,
        "decision": "PROCEED_TO_EXECUTION_SEMANTICS_DESIGN" if passed else "STOP_COMPRESSED_STRUCTURE_NO_GATE_PASS",
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "compressed_replay_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        f"# S006 EX14 执行\n\n状态：`COMPLETE`。固定复验21个组件、10个信息族、Q50单一路径；完整年化{full['cagr']:.2%}、最大回撤{full['maximum_drawdown']:.2%}、60日闭合交易中位数/P10为{full['rolling_60_closed_trades_median']:.1f}/{full['rolling_60_closed_trades_p10']:.1f}。未创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S006 EX14 结论\n\n裁决：`{evidence['decision']}`。压缩结构保留EX12完整年化的{evidence['cagr_retention_vs_ex12']:.1%}；发现期年化{discovery_metrics['cagr']:.2%}，确认期年化{confirmation_metrics['cagr']:.2%}、最大回撤{confirmation_metrics['maximum_drawdown']:.2%}。本轮是开发证据，不形成候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": evidence["decision"],
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
