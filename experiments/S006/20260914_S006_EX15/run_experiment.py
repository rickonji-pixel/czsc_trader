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


EXPERIMENT_ID = "20260914_S006_EX15"


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX15 protocol identity or return declaration differs")
    sources = protocol["sources"]
    frozen = {
        repo / str(sources["ex12_manifest_path"]): sources["ex12_manifest_sha256"],
        repo / str(sources["discovery_panel_path"]): sources["discovery_panel_sha256"],
        repo / str(sources["ex14_manifest_path"]): sources["ex14_manifest_sha256"],
        repo / str(sources["ex14_weights_path"]): sources["ex14_weights_sha256"],
        repo / str(sources["ex14_script_path"]): sources["ex14_script_sha256"],
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
    validate_experiment_archive((repo / str(sources["ex12_manifest_path"])).parent)
    validate_experiment_archive((repo / str(sources["ex14_manifest_path"])).parent)
    h6 = _module(repo / str(sources["ex06_script_path"]), "s006_ex15_h6")
    h10 = _module(repo / str(sources["ex10_script_path"]), "s006_ex15_h10")
    h12 = _module(repo / str(sources["ex12_script_path"]), "s006_ex15_h12")
    h14 = _module(repo / str(sources["ex14_script_path"]), "s006_ex15_h14")

    weights = pd.read_csv(repo / str(sources["ex14_weights_path"]))
    positive_families = set(weights.loc[weights["ridge_weight"] > 0, "information_family"].astype(str))
    discovery_panel = pd.read_csv(repo / str(sources["discovery_panel_path"]))
    eligible = discovery_panel.loc[discovery_panel["selected"] & discovery_panel["information_family"].isin(positive_families)].copy()
    count = int(protocol["selection"]["components_per_positive_family"])
    panel = (
        eligible.sort_values(["information_family", "oof_ic"], ascending=[True, False])
        .groupby("information_family", sort=True)
        .head(count)
        .reset_index(drop=True)
    )
    selection = protocol["selection"]
    if len(panel) != int(selection["expected_components"]) or panel["information_family"].nunique() != int(selection["expected_information_families"]):
        raise ValueError("bounded bridge panel dimensions differ")
    if len(panel) > int(selection["maximum_total_components"]):
        raise ValueError("bounded bridge exceeds component ceiling")
    panel.to_csv(artifacts / "preregistered_bridge_panel.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

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
    raw_components = h14._materialize(repo, protocol, panel, calendar, h6)
    apply_all = np.ones(len(calendar), dtype=bool)
    prior = float(protocol["architecture"]["categorical_shrinkage_prior_count"])
    component_scores: dict[str, pd.Series] = {}
    for component_id, row in panel.set_index("component_id").iterrows():
        kind, raw = raw_components[str(component_id)]
        component_scores[str(component_id)] = (
            h12._categorical_score(raw, outcome, discovery, apply_all, prior, h6)
            if kind == "CATEGORICAL_SIGNAL"
            else h12._continuous_score(raw, outcome, discovery, apply_all, h6)
        )
    scores = pd.DataFrame(component_scores, index=calendar)
    families = pd.DataFrame({
        str(family): scores[group["component_id"].astype(str).tolist()].median(axis=1, skipna=True)
        for family, group in panel.groupby("information_family", sort=True)
    }, index=calendar)
    train = pd.concat([families.loc[discovery], outcome.loc[discovery].rename("target")], axis=1).dropna()
    scaler = StandardScaler().fit(train[families.columns])
    ridge = Ridge(alpha=10.0, positive=True).fit(scaler.transform(train[families.columns]), train["target"])
    combined = pd.Series(
        ridge.predict(scaler.transform(families.fillna(families.loc[discovery].median()))),
        index=calendar,
    )
    threshold = float(combined.loc[discovery].quantile(float(protocol["architecture"]["entry_quantile"])))
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
    family_weights = pd.DataFrame({"information_family": families.columns, "ridge_weight": ridge.coef_})
    family_weights.to_csv(artifacts / "bridge_family_weights.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    row: dict[str, object] = {
        "path_id": "BRIDGE26:H1:POSITIVE_RIDGE_A10:S1:Q50",
        "entry_threshold": threshold,
        "components": len(panel),
        "information_families": len(families.columns),
        "hard_gates_pass": passed,
        "gate_checks_json": json.dumps(checks, sort_keys=True),
    }
    for prefix, metrics in (("full", full), ("discovery", discovery_metrics), ("confirmation", confirmation_metrics)):
        row.update({f"{prefix}_{key}": value for key, value in metrics.items()})
    pd.DataFrame([row]).to_csv(artifacts / "bridge_replay_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
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
        "hard_gate_checks": checks,
        "hard_gates_pass": passed,
        "decision": "PROCEED_TO_EXECUTION_SEMANTICS_DESIGN" if passed else "STOP_BOUNDED_COMPLEXITY_BRIDGE_NO_GATE_PASS",
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "bridge_replay_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        f"# S006 EX15 执行\n\n状态：`COMPLETE`。固定复验{len(panel)}个组件、{len(families.columns)}个信息族、Q50单一路径；完整年化{full['cagr']:.2%}、最大回撤{full['maximum_drawdown']:.2%}、60日闭合交易中位数/P10为{full['rolling_60_closed_trades_median']:.1f}/{full['rolling_60_closed_trades_p10']:.1f}。未创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S006 EX15 结论\n\n裁决：`{evidence['decision']}`。发现期年化{discovery_metrics['cagr']:.2%}，确认期年化{confirmation_metrics['cagr']:.2%}、最大回撤{confirmation_metrics['maximum_drawdown']:.2%}。本轮是开发证据，不形成候选。\n",
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
