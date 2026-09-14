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


EXPERIMENT_ID = "20260914_S006_EX12"


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


def _categorical_score(states: pd.Series, outcome: pd.Series, train_mask: np.ndarray, apply_mask: np.ndarray, prior: float, helpers) -> pd.Series:
    train = pd.concat([states.loc[train_mask].rename("state"), outcome.loc[train_mask].rename("outcome")], axis=1).dropna()
    baseline = float(train["outcome"].mean())
    edges: dict[str, float] = {}
    for state, group in train.groupby(train["state"].astype(str)):
        count = len(group)
        edges[str(state)] = float(group["outcome"].mean() - baseline) * count / (count + prior)
    raw_train = train["state"].astype(str).map(edges)
    apply_states = states.loc[apply_mask]
    raw_apply = apply_states.astype(str).map(edges).where(apply_states.notna())
    _, normalized = helpers._empirical_score(raw_train, raw_apply, 0.01, 0.99)
    return normalized


def _continuous_score(raw: pd.Series, outcome: pd.Series, train_mask: np.ndarray, apply_mask: np.ndarray, helpers) -> pd.Series:
    train_raw = raw.loc[train_mask]
    apply_raw = raw.loc[apply_mask]
    train_score, apply_score = helpers._empirical_score(train_raw, apply_raw, 0.01, 0.99)
    ic = helpers._spearman(train_score, outcome.loc[train_mask])
    orientation = -1.0 if ic is not None and ic < 0 else 1.0
    return apply_score * orientation


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX12 protocol identity or return declaration differs")
    sources = protocol["sources"]
    review = repo / "experiments/S006" / str(sources["review_experiment_id"])
    validate_experiment_archive(review)
    frozen = {
        review / "experiment_manifest.json": sources["review_manifest_sha256"],
        repo / str(sources["ex06_script_path"]): sources["ex06_script_sha256"],
        repo / str(sources["ex10_script_path"]): sources["ex10_script_sha256"],
        repo / str(sources["corrected_ledger_path"]): sources["corrected_ledger_sha256"],
        repo / str(sources["primary_states_path"]): sources["primary_states_sha256"],
        repo / str(sources["project_states_path"]): sources["project_states_sha256"],
        repo / str(sources["factor_ledger_path"]): sources["factor_ledger_sha256"],
        repo / str(sources["industry_path"]): sources["industry_sha256"],
        repo / str(sources["analyst_path"]): sources["analyst_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")
    h6 = _module(repo / str(sources["ex06_script_path"]), "s006_ex06_helpers")
    h10 = _module(repo / str(sources["ex10_script_path"]), "s006_ex10_helpers")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(context, str(protocol["dataset"]["name"]), str(protocol["symbol"]), str(protocol["asset_type"]), date.fromisoformat(str(protocol["development_cutoff"])))
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

    ledger = pd.read_csv(repo / str(sources["corrected_ledger_path"]))
    h1_ledger = ledger.loc[ledger["horizon_sessions"].eq(1) & ~ledger["kind"].eq("EVENT_FACTOR")].set_index("component_id")
    primary = pd.read_csv(repo / str(sources["primary_states_path"]))
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(calendar)
    categorical: dict[str, pd.Series] = {column: h6._shift_after_close(primary[column], calendar) for column in primary.columns}
    project_states = pd.read_csv(repo / str(sources["project_states_path"]), parse_dates=["first_usable_date"])
    for signal_id, group in project_states.groupby("signal_id", sort=True):
        raw = h6._map_first_usable(group, calendar, "state")
        raw.loc[raw.astype(str).eq("warmup")] = np.nan
        categorical[str(signal_id)] = raw

    factor_ledger = pd.read_csv(repo / str(sources["factor_ledger_path"]))
    numeric = factor_ledger.loc[factor_ledger["value_numeric"].notna()]
    continuous: dict[str, pd.Series] = {str(factor_id): h6._map_first_usable(group, calendar, "value_numeric").astype(float) for factor_id, group in numeric.groupby("factor_id", sort=True)}
    industry = pd.read_csv(repo / str(sources["industry_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
    breadth = h6._causal_percentile(industry["positive_member_ratio"], 252, 126)
    flow = h6._causal_percentile(industry["net_flow_ratio"], 252, 126)
    industry_value = h6._causal_percentile(pd.concat([breadth, flow], axis=1).mean(axis=1, skipna=False), 252, 126)
    industry_frame = pd.DataFrame({"first_usable_date": pd.Series(industry["trade_date"].to_numpy(), index=industry.index).map(lambda value: h6._next_calendar_session(calendar, pd.Timestamp(value))), "first_usable_clock": "09:00:00", "value_numeric": industry_value}).dropna(subset=["first_usable_date"])
    continuous["F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW"] = h6._map_first_usable(industry_frame, calendar, "value_numeric").astype(float)
    analyst = pd.read_csv(repo / str(sources["analyst_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
    continuous["F-PROJECT-SELL-SIDE-REVISION-BREADTH"] = h6._map_first_usable(pd.DataFrame({"first_usable_date": analyst["trade_date"], "first_usable_clock": "09:00:00", "value_numeric": analyst["revision_score"]}), calendar, "value_numeric").astype(float)
    if len(categorical) != 665 or len(continuous) != 14:
        raise ValueError("materialized component counts differ")

    selection = protocol["selection"]
    prior = float(selection["categorical_shrinkage_prior_count"])
    minimum = int(selection["minimum_observations_per_holdout_year"])
    selected: list[str] = []
    selection_rows: list[dict[str, object]] = []
    all_components: dict[str, tuple[str, pd.Series]] = {key: ("CATEGORICAL_SIGNAL", value) for key, value in categorical.items()}
    all_components.update({key: ("CONTINUOUS_FACTOR", value) for key, value in continuous.items()})
    for component_id, (kind, raw) in all_components.items():
        oof_parts: list[pd.Series] = []
        annual: dict[str, float | None] = {}
        for year in (2021, 2022, 2023):
            train_mask = discovery & (calendar.year != year)
            apply_mask = discovery & (calendar.year == year)
            scores = _categorical_score(raw, outcome, train_mask, apply_mask, prior, h6) if kind == "CATEGORICAL_SIGNAL" else _continuous_score(raw, outcome, train_mask, apply_mask, h6)
            frame = pd.concat([scores.rename("score"), outcome.loc[apply_mask].rename("outcome")], axis=1).dropna()
            ic = h6._spearman(frame["score"], frame["outcome"]) if len(frame) >= minimum else None
            annual[str(year)] = ic
            oof_parts.append(scores)
        oof = pd.concat(oof_parts).sort_index()
        pooled = h6._spearman(oof, outcome.reindex(oof.index))
        positive_years = sum(value is not None and value > 0 for value in annual.values())
        accepted = pooled is not None and pooled > 0 and positive_years >= int(selection["minimum_positive_holdout_years"])
        if accepted:
            selected.append(component_id)
        selection_rows.append({"component_id": component_id, "kind": kind, "information_family": str(h1_ledger.loc[component_id, "information_family"]), "oof_ic": pooled, "positive_holdout_years": positive_years, "annual_ic_json": json.dumps(annual, sort_keys=True), "selected": bool(accepted)})
    selection_frame = pd.DataFrame(selection_rows)
    selection_frame.to_csv(artifacts / "discovery_only_component_panel.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    if not selected:
        raise ValueError("discovery-only panel is empty")

    final_scores: dict[str, pd.Series] = {}
    for component_id in selected:
        kind, raw = all_components[component_id]
        final_scores[component_id] = _categorical_score(raw, outcome, discovery, np.ones(len(calendar), dtype=bool), prior, h6) if kind == "CATEGORICAL_SIGNAL" else _continuous_score(raw, outcome, discovery, np.ones(len(calendar), dtype=bool), h6)
    score_frame = pd.DataFrame(final_scores, index=calendar)
    family_scores: dict[str, pd.Series] = {}
    for family, group in selection_frame.loc[selection_frame["selected"]].groupby("information_family", sort=True):
        ids = group["component_id"].astype(str).tolist()
        family_scores[str(family)] = score_frame[ids].median(axis=1, skipna=True)
    families = pd.DataFrame(family_scores, index=calendar)
    train = pd.concat([families.loc[discovery], outcome.loc[discovery].rename("target")], axis=1).dropna()
    scaler = StandardScaler().fit(train[families.columns])
    ridge = Ridge(alpha=10.0, positive=True).fit(scaler.transform(train[families.columns]), train["target"])
    fill = families.loc[discovery].median()
    combined = pd.Series(ridge.predict(scaler.transform(families.fillna(fill))), index=calendar)
    weights = {column: float(value) for column, value in zip(families.columns, ridge.coef_, strict=True)}

    architecture = protocol["architecture"]
    fee = float(protocol["execution"]["fee_rate_one_way"])
    gates = protocol["hard_gates"]
    rows: list[dict[str, object]] = []
    for quantile in map(float, architecture["entry_quantiles"]):
        threshold = float(combined.loc[discovery].quantile(quantile))
        positions = combined.ge(threshold).astype(float)
        full = h10._metrics(positions, opens, fee)
        discovery_metrics = h10._metrics(positions.loc[discovery], opens.loc[discovery], fee)
        confirmation_metrics = h10._metrics(positions.loc[confirmation], opens.loc[confirmation], fee)
        passed = full["cagr"] >= float(gates["minimum_cagr"]) and full["maximum_drawdown"] >= float(gates["maximum_drawdown_limit"]) and full["maximum_drawdown"] > float(gates["s001_v2_maximum_drawdown"]) and full["rolling_60_closed_trades_median"] >= float(gates["minimum_rolling_60_closed_trades_median"]) and full["rolling_60_closed_trades_p10"] >= float(gates["minimum_rolling_60_closed_trades_p10"]) and confirmation_metrics["cagr"] > float(gates["minimum_confirmation_cagr"]) and confirmation_metrics["maximum_drawdown"] >= float(gates["confirmation_maximum_drawdown_limit"])
        row = {"path_id": f"DISCOVERY_CROSSYEAR:H1:POSITIVE_RIDGE_A10:S1:Q{int(quantile*100)}", "entry_quantile": quantile, "entry_threshold": threshold, "selected_components": len(selected), "information_families": len(families.columns), "weights_json": json.dumps(weights, sort_keys=True), "hard_gates_pass": bool(passed)}
        for prefix, metrics in (("full", full), ("discovery", discovery_metrics), ("confirmation", confirmation_metrics)):
            row.update({f"{prefix}_{key}": value for key, value in metrics.items()})
        rows.append(row)
    results = pd.DataFrame(rows)
    if len(results) != int(architecture["expected_paths"]):
        raise ValueError("architecture path count differs")
    results = results.sort_values(["hard_gates_pass", "full_cagr", "full_calmar"], ascending=[False, False, False])
    results.to_csv(artifacts / "discovery_only_replay_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    qualified = results.loc[results["hard_gates_pass"]]
    evidence = {"schema_version": 1, "experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "catalog_components_considered": len(all_components), "selected_components": len(selected), "selected_information_families": len(families.columns), "search_paths": len(results), "qualifying_paths": len(qualified), "best_path_id": str(results.iloc[0]["path_id"]), "best_full_cagr": float(results.iloc[0]["full_cagr"]), "best_full_maximum_drawdown": float(results.iloc[0]["full_maximum_drawdown"]), "best_frequency_median": float(results.iloc[0]["full_rolling_60_closed_trades_median"]), "best_frequency_p10": float(results.iloc[0]["full_rolling_60_closed_trades_p10"]), "best_discovery_cagr": float(results.iloc[0]["discovery_cagr"]), "best_confirmation_cagr": float(results.iloc[0]["confirmation_cagr"]), "decision": "PROCEED_TO_PARAMETER_PLATFORM_AUDIT" if len(qualified) else "STOP_DISCOVERY_ONLY_PANEL_NO_GATE_PASS", "candidate_created": False, "strategy_manager_mutated": False, "pte_mutated": False}
    _write(artifacts / "discovery_only_replay_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(f"# S006 EX12 执行\n\n状态：`COMPLETE`。从679个已物化组件中，仅用发现期跨年度留一证据选出{len(selected)}个组件、{len(families.columns)}个信息族；完成6条固定阈值回放，其中{len(qualified)}条通过硬门。未创建候选或修改SM/PTE。\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(f"# S006 EX12 结论\n\n裁决：`{evidence['decision']}`。最佳路径`{evidence['best_path_id']}`：完整年化{evidence['best_full_cagr']:.2%}、最大回撤{evidence['best_full_maximum_drawdown']:.2%}、频率{evidence['best_frequency_median']:.1f}/{evidence['best_frequency_p10']:.1f}；发现期年化{evidence['best_discovery_cagr']:.2%}、确认期年化{evidence['best_confirmation_cagr']:.2%}。本轮仍属于开发证据，不形成候选。\n", encoding="utf-8")
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": protocol["strategy_id"], "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": evidence["decision"], "promotion_allowed": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
