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


EXPERIMENT_ID = "20260914_S006_EX10"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_helpers(path: Path):
    spec = importlib.util.spec_from_file_location("s006_ex06_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load EX06 helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metrics(positions: pd.Series, opens: pd.Series, fee: float) -> dict[str, object]:
    positions = positions.reindex(opens.index).fillna(0.0).astype(float)
    gross = opens.shift(-1).div(opens).sub(1.0).fillna(0.0)
    turnover = positions.diff().abs().fillna(positions.abs())
    returns = positions * gross - turnover * fee
    equity = (1.0 + returns).cumprod()
    years = max(len(returns) / 252.0, 1.0 / 252.0)
    cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0)
    drawdown = equity.div(equity.cummax()).sub(1.0)
    maximum_drawdown = float(drawdown.min())
    exits = positions.shift(1, fill_value=0).gt(0) & positions.eq(0)
    rolling = exits.astype(int).rolling(60, min_periods=60).sum().dropna()
    trade_returns: list[float] = []
    start: int | None = None
    values = returns.to_numpy(dtype=float)
    flags = positions.to_numpy(dtype=float)
    for index, value in enumerate(flags):
        if value > 0 and (index == 0 or flags[index - 1] == 0):
            start = index
        if value == 0 and index > 0 and flags[index - 1] > 0 and start is not None:
            trade_returns.append(float(np.prod(1.0 + values[start:index + 1]) - 1.0))
            start = None
    gains = sum(value for value in trade_returns if value > 0)
    losses = -sum(value for value in trade_returns if value < 0)
    annual = returns.groupby(returns.index.year).apply(lambda values_: float((1.0 + values_).prod() - 1.0))
    return {
        "total_return": float(equity.iloc[-1] - 1.0),
        "cagr": cagr,
        "maximum_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else None,
        "closed_trades": int(exits.sum()),
        "rolling_60_closed_trades_median": float(rolling.median()) if len(rolling) else 0.0,
        "rolling_60_closed_trades_p10": float(rolling.quantile(0.10)) if len(rolling) else 0.0,
        "exposure_ratio": float(positions.mean()),
        "profit_factor": gains / losses if losses > 0 else None,
        "positive_years": int((annual > 0).sum()),
        "annual_returns_json": json.dumps({str(int(key)): value for key, value in annual.items()}, sort_keys=True),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX10 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX10 cannot create, promote, or deploy a candidate")
    sources = protocol["sources"]
    anchor = repo / "experiments/S006" / str(sources["anchor_audit_experiment_id"])
    validate_experiment_archive(anchor)
    frozen = {
        anchor / "experiment_manifest.json": sources["anchor_audit_manifest_sha256"],
        repo / str(sources["corrected_ledger_path"]): sources["corrected_ledger_sha256"],
        repo / str(sources["ex06_script_path"]): sources["ex06_script_sha256"],
        repo / str(sources["state_models_path"]): sources["state_models_sha256"],
        repo / str(sources["continuous_models_path"]): sources["continuous_models_sha256"],
        repo / str(sources["primary_states_path"]): sources["primary_states_sha256"],
        repo / str(sources["project_states_path"]): sources["project_states_sha256"],
        repo / str(sources["factor_ledger_path"]): sources["factor_ledger_sha256"],
        repo / str(sources["industry_path"]): sources["industry_sha256"],
        repo / str(sources["analyst_path"]): sources["analyst_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")
    helpers = _load_helpers(repo / str(sources["ex06_script_path"]))

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
    dataset = protocol["dataset"]
    discovery = (calendar >= pd.Timestamp(dataset["discovery_start"])) & (calendar <= pd.Timestamp(dataset["discovery_end"]))
    confirmation = (calendar >= pd.Timestamp(dataset["confirmation_start"])) & (calendar <= pd.Timestamp(dataset["confirmation_end"]))

    ledger = pd.read_csv(repo / str(sources["corrected_ledger_path"]))
    state_models = pd.read_csv(repo / str(sources["state_models_path"]))
    continuous_models = pd.read_csv(repo / str(sources["continuous_models_path"]))
    primary = pd.read_csv(repo / str(sources["primary_states_path"]))
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(calendar)
    project_states = pd.read_csv(repo / str(sources["project_states_path"]), parse_dates=["first_usable_date"])
    categorical_raw: dict[str, pd.Series] = {column: helpers._shift_after_close(primary[column], calendar) for column in primary.columns}
    for signal_id, group in project_states.groupby("signal_id", sort=True):
        raw = helpers._map_first_usable(group, calendar, "state")
        raw.loc[raw.astype(str).eq("warmup")] = np.nan
        categorical_raw[str(signal_id)] = raw

    factor_ledger = pd.read_csv(repo / str(sources["factor_ledger_path"]))
    numeric = factor_ledger.loc[factor_ledger["value_numeric"].notna()].copy()
    continuous_raw: dict[str, pd.Series] = {
        str(factor_id): helpers._map_first_usable(group, calendar, "value_numeric").astype(float)
        for factor_id, group in numeric.groupby("factor_id", sort=True)
    }
    industry = pd.read_csv(repo / str(sources["industry_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
    industry_breadth = helpers._causal_percentile(industry["positive_member_ratio"], 252, 126)
    industry_flow = helpers._causal_percentile(industry["net_flow_ratio"], 252, 126)
    industry_value = helpers._causal_percentile(pd.concat([industry_breadth, industry_flow], axis=1).mean(axis=1, skipna=False), 252, 126)
    industry_frame = pd.DataFrame({"first_usable_date": pd.Series(industry["trade_date"].to_numpy(), index=industry.index).map(lambda value: helpers._next_calendar_session(calendar, pd.Timestamp(value))), "first_usable_clock": "09:00:00", "value_numeric": industry_value}).dropna(subset=["first_usable_date"])
    continuous_raw["F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW"] = helpers._map_first_usable(industry_frame, calendar, "value_numeric").astype(float)
    analyst = pd.read_csv(repo / str(sources["analyst_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
    analyst_frame = pd.DataFrame({"first_usable_date": analyst["trade_date"], "first_usable_clock": "09:00:00", "value_numeric": analyst["revision_score"]})
    continuous_raw["F-PROJECT-SELL-SIDE-REVISION-BREADTH"] = helpers._map_first_usable(analyst_frame, calendar, "value_numeric").astype(float)
    if len(categorical_raw) != 665 or len(continuous_raw) != 14:
        raise ValueError(f"materialized component count differs: {len(categorical_raw)}, {len(continuous_raw)}")

    search = protocol["search"]
    fee = float(protocol["execution"]["fee_rate_one_way"])
    gate = protocol["hard_gates"]
    rows: list[dict[str, object]] = []
    family_rows: list[dict[str, object]] = []
    for horizon in map(int, search["horizons"]):
        path_ledger = ledger.loc[ledger["horizon_sessions"].eq(horizon) & ~ledger["kind"].eq("EVENT_FACTOR")].copy()
        component_scores: dict[str, pd.Series] = {}
        for component_id, raw_states in categorical_raw.items():
            model = state_models.loc[(state_models["component_id"].eq(component_id)) & (state_models["horizon_sessions"].eq(horizon))]
            mapping = dict(zip(model["state"].astype(str), model["frozen_score"].astype(float), strict=True))
            raw_score = raw_states.astype(str).map(mapping).where(raw_states.notna()).astype(float)
            _, normalized = helpers._empirical_score(raw_score.loc[discovery], raw_score, 0.01, 0.99)
            component_scores[component_id] = normalized
        for factor_id, raw in continuous_raw.items():
            _, normalized = helpers._empirical_score(raw.loc[discovery], raw, 0.01, 0.99)
            orientation = int(continuous_models.loc[(continuous_models["factor_id"].eq(factor_id)) & (continuous_models["horizon_sessions"].eq(horizon)), "orientation"].iloc[0])
            component_scores[factor_id] = normalized * orientation
        score_frame = pd.DataFrame(component_scores, index=calendar)

        for panel in search["panels"]:
            if panel == "ALL_MATERIALIZED":
                selected = path_ledger
            elif panel == "IDENTIFIABLE":
                selected = path_ledger.loc[path_ledger["identifiable"]]
            elif panel == "STABLE":
                selected = path_ledger.loc[path_ledger["evidence_label"].isin(["DIRECTIONALLY_STABLE", "NOMINAL_SUPPORT", "FDR_SUPPORTED"])]
            else:
                raise ValueError(f"unknown panel: {panel}")
            family_scores: dict[str, pd.Series] = {}
            for family, group in selected.groupby("information_family", sort=True):
                ids = [value for value in group["component_id"].astype(str).unique() if value in score_frame.columns]
                if ids:
                    family_scores[str(family)] = score_frame[ids].median(axis=1, skipna=True)
                    family_rows.append({"horizon_sessions": horizon, "panel": panel, "information_family": family, "components": len(ids)})
            families = pd.DataFrame(family_scores, index=calendar)
            if families.empty:
                raise ValueError(f"empty family panel: {panel}, H{horizon}")
            target = opens.shift(-horizon).div(opens).sub(1.0)
            for combiner in search["combiners"]:
                if combiner == "EQUAL_FAMILY":
                    combined = families.mean(axis=1, skipna=True)
                    weights = {column: 1.0 / len(families.columns) for column in families.columns}
                elif combiner == "POSITIVE_RIDGE_A10":
                    train = pd.concat([families.loc[discovery], target.loc[discovery].rename("target")], axis=1).dropna()
                    scaler = StandardScaler().fit(train[families.columns])
                    model = Ridge(alpha=10.0, positive=True).fit(scaler.transform(train[families.columns]), train["target"])
                    combined = pd.Series(model.predict(scaler.transform(families.fillna(families.loc[discovery].median()))), index=calendar)
                    weights = {column: float(value) for column, value in zip(families.columns, model.coef_, strict=True)}
                else:
                    raise ValueError(f"unknown combiner: {combiner}")
                for smoothing in map(int, search["smoothing_sessions"]):
                    smoothed = combined.rolling(smoothing, min_periods=smoothing).mean()
                    for quantile in map(float, search["entry_quantiles"]):
                        threshold = float(smoothed.loc[discovery].dropna().quantile(quantile))
                        positions = smoothed.ge(threshold).astype(float).where(smoothed.notna(), 0.0)
                        full_metrics = _metrics(positions, opens, fee)
                        discovery_metrics = _metrics(positions.loc[discovery], opens.loc[discovery], fee)
                        confirmation_metrics = _metrics(positions.loc[confirmation], opens.loc[confirmation], fee)
                        passes = (
                            full_metrics["cagr"] >= float(gate["minimum_cagr"])
                            and full_metrics["maximum_drawdown"] >= float(gate["maximum_drawdown_limit"])
                            and full_metrics["maximum_drawdown"] > float(gate["s001_v2_maximum_drawdown"])
                            and full_metrics["rolling_60_closed_trades_median"] >= float(gate["minimum_rolling_60_closed_trades_median"])
                            and full_metrics["rolling_60_closed_trades_p10"] >= float(gate["minimum_rolling_60_closed_trades_p10"])
                            and confirmation_metrics["cagr"] > float(gate["minimum_confirmation_cagr"])
                            and confirmation_metrics["maximum_drawdown"] >= float(gate["confirmation_maximum_drawdown_limit"])
                        )
                        row = {"path_id": f"{panel}:H{horizon}:{combiner}:S{smoothing}:Q{int(quantile*100)}", "panel": panel, "horizon_sessions": horizon, "combiner": combiner, "smoothing_sessions": smoothing, "entry_quantile": quantile, "entry_threshold": threshold, "information_families": len(families.columns), "components": int(selected["component_id"].nunique()), "weights_json": json.dumps(weights, sort_keys=True)}
                        for prefix, metrics in (("full", full_metrics), ("discovery", discovery_metrics), ("confirmation", confirmation_metrics)):
                            row.update({f"{prefix}_{key}": value for key, value in metrics.items()})
                        row["hard_gates_pass"] = bool(passes)
                        rows.append(row)

    results = pd.DataFrame(rows)
    if len(results) != int(search["expected_paths"]) or results["path_id"].nunique() != int(search["expected_paths"]):
        raise ValueError(f"search path count differs: {len(results)}")
    results = results.sort_values(["hard_gates_pass", "full_cagr", "full_calmar"], ascending=[False, False, False])
    results.to_csv(artifacts / "architecture_search_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.DataFrame(family_rows).drop_duplicates().to_csv(artifacts / "family_panel_coverage.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    qualifying = results.loc[results["hard_gates_pass"]]
    qualifying.to_csv(artifacts / "qualifying_architectures.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    evidence = {"schema_version": 1, "experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "search_paths": int(len(results)), "qualifying_paths": int(len(qualifying)), "best_path_id": str(results.iloc[0]["path_id"]), "best_full_cagr": float(results.iloc[0]["full_cagr"]), "best_full_maximum_drawdown": float(results.iloc[0]["full_maximum_drawdown"]), "best_frequency_median": float(results.iloc[0]["full_rolling_60_closed_trades_median"]), "best_frequency_p10": float(results.iloc[0]["full_rolling_60_closed_trades_p10"]), "decision": "PROCEED_TO_LIMITED_STRUCTURE_REPLICATION" if len(qualifying) else "STOP_FAMILY_BALANCED_ARCHITECTURE_NO_GATE_PASS", "candidate_created": False, "strategy_manager_mutated": False, "pte_mutated": False}
    _write(artifacts / "architecture_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(f"# S006 EX10 执行\n\n状态：`COMPLETE`。完成{len(results)}条预注册多变量结构路径，其中{len(qualifying)}条通过全部硬门。所有组件先在信息族内聚合，再由信息族平权参与。未创建候选或修改SM/PTE。\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(f"# S006 EX10 结论\n\n裁决：`{evidence['decision']}`。最佳路径`{evidence['best_path_id']}`，完整开发池年化{evidence['best_full_cagr']:.2%}、最大回撤{evidence['best_full_maximum_drawdown']:.2%}、滚动60日闭合交易中位数{evidence['best_frequency_median']:.1f}、P10为{evidence['best_frequency_p10']:.1f}。本轮只评估架构，不形成候选。\n", encoding="utf-8")
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": protocol["strategy_id"], "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": evidence["decision"], "promotion_allowed": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
