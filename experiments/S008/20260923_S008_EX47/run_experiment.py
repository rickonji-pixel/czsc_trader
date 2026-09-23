from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX47"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import frozen module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_prices(repo: Path, expected_manifest_sha256: str) -> pd.DataFrame:
    manifest_path = repo / "data/raw/518880_execution_manifest.json"
    if _sha256(manifest_path) != expected_manifest_sha256:
        raise ValueError("execution manifest differs from frozen identity")
    manifest = _read(manifest_path)
    frames = []
    for year in range(2013, 2025):
        name = f"518880_execution_daily_{year}.csv"
        path = repo / "data/raw" / name
        if _sha256(path) != manifest["files"][name]["sha256"]:
            raise ValueError(f"execution daily file differs from manifest: {name}")
        frames.append(pd.read_csv(path))
    prices = pd.concat(frames, ignore_index=True)
    prices["Date"] = pd.to_datetime(prices.pop("date"), errors="raise")
    return prices.sort_values("Date").set_index("Date")


def _select(ledger: pd.DataFrame, group_column: str | None, group: str, return_column: str, drawdown_column: str) -> pd.Series:
    eligible = ledger.loc[ledger["maximum_drawdown_gate_pass"].astype(bool)].copy()
    if group_column is not None:
        eligible = eligible.loc[eligible[group_column].eq(group)]
    if eligible.empty:
        raise ValueError(f"no eligible representative for {group}")
    return eligible.sort_values(
        [return_column, drawdown_column, "trial_number"],
        ascending=[False, True, True],
        kind="stable",
    ).iloc[0]


def _verify_representative(name: str, row: pd.Series, frozen: dict[str, object]) -> None:
    if int(row["trial_number"]) != int(frozen["trial_number"]) or str(row["behavior_sha256"]) != str(frozen["behavior_sha256"]):
        raise ValueError(f"representative selection differs for {name}")


def _oriented_normalized(panel: pd.DataFrame, orientations: dict[str, int], module: ModuleType) -> pd.DataFrame:
    normalized = pd.DataFrame(index=panel.index)
    for feature, orientation in orientations.items():
        value = module._percentile(panel[feature], 252, 126)
        normalized[feature] = value if int(orientation) > 0 else 1.0 - value
    return normalized


def _open_positions(targets: pd.Series, prices: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.Series:
    shifted = targets.reindex(prices.index).shift(1)
    positions = shifted.reindex(dates)
    if positions.isna().any() or not positions.isin([0, 1]).all():
        raise ValueError("representative cannot be aligned to oracle execution dates")
    return positions.astype("int8")


def _metrics(name: str, positions: pd.Series, oracle: pd.Series, open_returns: pd.Series, annualized_return: float, maximum_drawdown: float) -> dict[str, object]:
    dates = open_returns.index
    strategy = positions.reindex(dates).astype(bool)
    ideal = oracle.reindex(dates).astype(bool)
    positive = open_returns > 0
    negative = open_returns < 0
    positive_total = float(open_returns.loc[positive].sum())
    negative_total = float(abs(open_returns.loc[negative].sum()))
    positive_capture = float(open_returns.loc[positive & strategy].sum()) / positive_total
    negative_avoidance = 1.0 - float(abs(open_returns.loc[negative & strategy].sum())) / negative_total
    ideal_positive_capture = float(open_returns.loc[positive & ideal].sum()) / positive_total
    ideal_negative_avoidance = 1.0 - float(abs(open_returns.loc[negative & ideal].sum())) / negative_total
    oracle_long = ideal
    oracle_cash = ~ideal
    missed_oracle_positive = float(open_returns.loc[oracle_long & ~strategy & positive].sum())
    suffered_oracle_cash_negative = float(abs(open_returns.loc[oracle_cash & strategy & negative].sum()))
    return {
        "representative": name,
        "annualized_return": annualized_return,
        "maximum_drawdown_magnitude": maximum_drawdown,
        "exposure_ratio": float(strategy.mean()),
        "position_agreement_ratio": float((strategy == ideal).mean()),
        "oracle_long_recall": float(strategy.loc[oracle_long].mean()),
        "oracle_cash_recall": float((~strategy.loc[oracle_cash]).mean()),
        "false_cash_sessions": int((oracle_long & ~strategy).sum()),
        "false_long_sessions": int((oracle_cash & strategy).sum()),
        "positive_log_return_capture_ratio": positive_capture,
        "negative_log_return_avoidance_ratio": negative_avoidance,
        "positive_capture_deficit_vs_oracle": ideal_positive_capture - positive_capture,
        "negative_avoidance_deficit_vs_oracle": ideal_negative_avoidance - negative_avoidance,
        "missed_positive_log_return_during_oracle_long": missed_oracle_positive,
        "suffered_negative_log_return_during_oracle_cash": suffered_oracle_cash_negative,
        "gross_log_return_gap_annualized": float(((ideal.astype(int) - strategy.astype(int)) * open_returns).sum() * 252 / len(open_returns)),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = ("reads_sealed_validation", "starts_search", "new_mechanism", "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_platform", "mutates_pte")
    if not protocol.get("reads_development_returns") or any(protocol.get(key) for key in prohibited):
        raise ValueError("oracle attribution scope exceeds contract")

    roots = {number: repo / f"experiments/S008/20260923_S008_EX{number}" for number in (16, 38, 41, 42, 45, 46)}
    for root in roots.values():
        validate_experiment_archive(root)
    source_paths = {
        "ex16_manifest_sha256": roots[16] / "experiment_manifest.json",
        "feature_panel_sha256": roots[16] / "artifacts/causal_feature_panel.csv.gz",
        "ex38_manifest_sha256": roots[38] / "experiment_manifest.json",
        "ex38_ledger_sha256": roots[38] / "artifacts/search_trial_ledger.csv.gz",
        "ex38_runtime_sha256": roots[38] / "runtime/strategy_runtime/strategies/s008_prototypes.py",
        "ex41_manifest_sha256": roots[41] / "experiment_manifest.json",
        "ex41_ledger_sha256": roots[41] / "artifacts/discovery_grid_ledger.csv.gz",
        "ex41_runner_sha256": roots[41] / "run_experiment.py",
        "ex42_manifest_sha256": roots[42] / "experiment_manifest.json",
        "oracle_paths_sha256": roots[42] / "artifacts/oracle_paths.csv.gz",
        "oracle_ledger_sha256": roots[42] / "artifacts/oracle_budget_ledger.csv",
        "ex45_manifest_sha256": roots[45] / "experiment_manifest.json",
        "ex45_ledger_sha256": roots[45] / "artifacts/discovery_grid_ledger.csv.gz",
        "ex45_runner_sha256": roots[45] / "run_experiment.py",
        "ex46_manifest_sha256": roots[46] / "experiment_manifest.json",
        "ex46_ledger_sha256": roots[46] / "artifacts/discovery_grid_ledger.csv.gz",
        "ex46_runner_sha256": roots[46] / "run_experiment.py",
        "execution_manifest_sha256": repo / "data/raw/518880_execution_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")

    raw_panel = pd.read_csv(source_paths["feature_panel_sha256"])
    dates = pd.to_datetime(raw_panel.pop("Date"), errors="raise")
    raw_panel.index = dates
    panel = raw_panel.sort_index()
    prices = _load_prices(repo, protocol["sources"]["execution_manifest_sha256"])
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and execution calendar differ")
    oracle_paths = pd.read_csv(source_paths["oracle_paths_sha256"])
    oracle_paths["Date"] = pd.to_datetime(oracle_paths["Date"], errors="raise")
    oracle_frame = oracle_paths.loc[
        oracle_paths["cadence"].eq(protocol["oracle"]["cadence"])
        & oracle_paths["maximum_entry_budget"].eq(int(protocol["oracle"]["maximum_entry_budget"]))
    ].set_index("Date").sort_index()
    oracle = oracle_frame["target_position"].astype("int8")
    if hashlib.sha256(oracle.to_numpy(dtype="int8").tobytes()).hexdigest() != protocol["oracle"]["behavior_sha256"]:
        raise ValueError("oracle behavior differs from protocol")
    oracle_dates = pd.DatetimeIndex(oracle.index)
    if oracle_dates[0] != pd.Timestamp(protocol["evaluation_start"]) or oracle_dates[-1] != pd.Timestamp(protocol["evaluation_end"]):
        raise ValueError("oracle evaluation window differs from protocol")

    frozen_representatives = protocol["representatives"]
    selection_rows: list[dict[str, object]] = []
    positions: dict[str, pd.Series] = {}

    ledger38 = pd.read_csv(source_paths["ex38_ledger_sha256"])
    runtime38 = _module(source_paths["ex38_runtime_sha256"], "s008_ex47_runtime38")
    normalized38 = runtime38.normalize_components(panel.loc[:, list(runtime38._ORIENTATIONS)], 252, 126)
    for name, group in (("P01_COMPOSITE_GATE", "S008-P01-COMPOSITE-GATE"), ("P02_REGIME_RECOVERY", "S008-P02-REGIME-RECOVERY"), ("P03_SAFE_HAVEN_PULSE", "S008-P03-SAFE-HAVEN-PULSE")):
        row = _select(ledger38, "prototype_id", group, "annualized_return_objective", "maximum_drawdown_magnitude_objective")
        _verify_representative(name, row, frozen_representatives[name])
        rule = json.loads(row["effective_parameters_json"])
        scores = runtime38._common_scores(normalized38, rule)
        if group == "S008-P01-COMPOSITE-GATE":
            history = runtime38._composite_gate(scores, rule)
        elif group == "S008-P02-REGIME-RECOVERY":
            history = runtime38._regime_recovery(scores, rule)
        else:
            history = runtime38._safe_haven_pulse(normalized38, scores, rule)
        signal_dates = prices.index[(prices.index >= pd.Timestamp(row["evaluation_start"])) & (prices.index < pd.Timestamp(row["evaluation_end"]))]
        selected_targets = history.loc[signal_dates, "target_position"].astype("int8")
        if hashlib.sha256(selected_targets.to_numpy(dtype="int8").tobytes()).hexdigest() != row["behavior_sha256"]:
            raise ValueError(f"reconstructed behavior differs for {name}")
        positions[name] = pd.Series(selected_targets.to_numpy(), index=oracle_dates, dtype="int8")
        selection_rows.append({"representative": name, "experiment": "EX38", "trial_number": int(row["trial_number"]), "annualized_return": float(row["annualized_return_objective"]), "maximum_drawdown_magnitude": float(row["maximum_drawdown_magnitude_objective"]), "behavior_sha256": row["behavior_sha256"]})

    module41 = _module(source_paths["ex41_runner_sha256"], "s008_ex47_runner41")
    ledger41 = pd.read_csv(source_paths["ex41_ledger_sha256"])
    row41 = _select(ledger41, None, "ALL", "annualized_return", "maximum_drawdown_magnitude")
    name41 = "P04_CORE_CARRY_DRAWDOWN_SHIELD"
    _verify_representative(name41, row41, frozen_representatives[name41])
    protocol41 = _read(roots[41] / "artifacts/protocol.json")
    normalized41 = _oriented_normalized(panel, {str(k): int(v) for k, v in protocol41["risk_components"].items()}, module41)
    start41 = normalized41.notna().all(axis=1).loc[lambda value: value].index[0]
    normalized41 = normalized41.loc[start41:]
    rule41 = json.loads(row41["parameters_json"])
    scores41 = pd.DataFrame({"risk_score": normalized41.mean(axis=1), "price_return_5d": panel.loc[normalized41.index, "price_return_5d"]})
    scores41["high_risk_channels"] = normalized41.ge(float(rule41["channel_threshold"])).sum(axis=1)
    targets41 = module41._targets(scores41, rule41)
    if hashlib.sha256(targets41.to_numpy(dtype="int8").tobytes()).hexdigest() != row41["behavior_sha256"]:
        raise ValueError("reconstructed behavior differs for P04")
    positions[name41] = _open_positions(targets41, prices, oracle_dates)
    selection_rows.append({"representative": name41, "experiment": "EX41", "trial_number": int(row41["trial_number"]), "annualized_return": float(row41["annualized_return"]), "maximum_drawdown_magnitude": float(row41["maximum_drawdown_magnitude"]), "behavior_sha256": row41["behavior_sha256"]})

    module45 = _module(source_paths["ex45_runner_sha256"], "s008_ex47_runner45")
    ledger45 = pd.read_csv(source_paths["ex45_ledger_sha256"])
    protocol45 = _read(roots[45] / "artifacts/protocol.json")
    orientations45 = {str(k): int(v) for k, v in protocol45["orientations"].items()}
    normalized45 = _oriented_normalized(panel, orientations45, module45)
    start45 = normalized45.notna().all(axis=1).loc[lambda value: value].index[0]
    normalized45 = normalized45.loc[start45:]
    equity45 = list(protocol45["components"]["equity"])
    scores45 = pd.DataFrame(index=normalized45.index)
    scores45["currency"] = normalized45[str(protocol45["components"]["currency"])]
    scores45["equity_return"] = normalized45[equity45[0]]
    scores45["equity_drawdown"] = normalized45[equity45[1]]
    scores45["equity_turnover"] = normalized45[equity45[2]]
    scores45["equity_score"] = normalized45[equity45].mean(axis=1)
    for name, group in (("P05_TRANSLATION_LED", "TRANSLATION_LED"), ("P05_RISK_OFF_LED", "RISK_OFF_LED")):
        row = _select(ledger45, "prototype", group, "annualized_return", "maximum_drawdown_magnitude")
        _verify_representative(name, row, frozen_representatives[name])
        targets = module45._targets(scores45, group, json.loads(row["parameters_json"]))
        if hashlib.sha256(targets.to_numpy(dtype="int8").tobytes()).hexdigest() != row["behavior_sha256"]:
            raise ValueError(f"reconstructed behavior differs for {name}")
        positions[name] = _open_positions(targets, prices, oracle_dates)
        selection_rows.append({"representative": name, "experiment": "EX45", "trial_number": int(row["trial_number"]), "annualized_return": float(row["annualized_return"]), "maximum_drawdown_magnitude": float(row["maximum_drawdown_magnitude"]), "behavior_sha256": row["behavior_sha256"]})

    module46 = _module(source_paths["ex46_runner_sha256"], "s008_ex47_runner46")
    ledger46 = pd.read_csv(source_paths["ex46_ledger_sha256"])
    row46 = _select(ledger46, None, "ALL", "annualized_return", "maximum_drawdown_magnitude")
    name46 = "P06_CORE_CARRY_REGIME_VETO"
    _verify_representative(name46, row46, frozen_representatives[name46])
    protocol46 = _read(roots[46] / "artifacts/protocol.json")
    orientations46 = {str(k): int(v) for k, v in protocol46["favorable_components"].items()}
    normalized46 = _oriented_normalized(panel, orientations46, module46)
    start46 = normalized46.notna().all(axis=1).loc[lambda value: value].index[0]
    normalized46 = normalized46.loc[start46:]
    rule46 = json.loads(row46["parameters_json"])
    equity46 = [name for name in orientations46 if name != "currency_usdcnh_return_20d"]
    scores46 = pd.DataFrame(index=normalized46.index)
    weight46 = float(rule46["currency_weight"])
    scores46["regime_score"] = weight46 * normalized46["currency_usdcnh_return_20d"] + (1.0 - weight46) * normalized46[equity46].mean(axis=1)
    for feature in protocol46["price_components"]:
        scores46[str(feature)] = panel.loc[scores46.index, str(feature)]
    targets46 = module46._targets(scores46, rule46)
    if hashlib.sha256(targets46.to_numpy(dtype="int8").tobytes()).hexdigest() != row46["behavior_sha256"]:
        raise ValueError("reconstructed behavior differs for P06")
    positions[name46] = _open_positions(targets46, prices, oracle_dates)
    selection_rows.append({"representative": name46, "experiment": "EX46", "trial_number": int(row46["trial_number"]), "annualized_return": float(row46["annualized_return"]), "maximum_drawdown_magnitude": float(row46["maximum_drawdown_magnitude"]), "behavior_sha256": row46["behavior_sha256"]})

    selection = pd.DataFrame(selection_rows).sort_values("representative").reset_index(drop=True)
    comparison_positions = pd.DataFrame({"oracle": oracle, **positions}, index=oracle_dates)
    opens = prices.loc[oracle_dates, "open"].astype(float)
    open_returns = np.log(opens.shift(-1) / opens).dropna()
    metric_rows = []
    for row in selection.itertuples():
        metric_rows.append(_metrics(row.representative, positions[row.representative], oracle, open_returns, float(row.annualized_return), float(row.maximum_drawdown_magnitude)))
    metrics = pd.DataFrame(metric_rows).sort_values("annualized_return", ascending=False).reset_index(drop=True)

    segment_ids = oracle.ne(oracle.shift()).cumsum()
    segment_rows: list[dict[str, object]] = []
    for segment_id, segment in oracle.groupby(segment_ids):
        segment_dates = segment.index
        valid_dates = segment_dates.intersection(open_returns.index)
        for name, strategy in positions.items():
            segment_rows.append({
                "segment_id": int(segment_id),
                "oracle_state": int(segment.iloc[0]),
                "start": segment_dates[0].date().isoformat(),
                "end": segment_dates[-1].date().isoformat(),
                "sessions": len(segment_dates),
                "representative": name,
                "strategy_exposure": float(strategy.loc[segment_dates].mean()),
                "state_agreement": float(strategy.loc[segment_dates].eq(segment.iloc[0]).mean()),
                "segment_open_log_return": float(open_returns.reindex(valid_dates).sum()),
                "strategy_held_open_log_return": float((open_returns.reindex(valid_dates) * strategy.reindex(valid_dates)).sum()),
            })
    segments = pd.DataFrame(segment_rows)

    transition_rows: list[dict[str, object]] = []
    transition_dates = oracle.index[oracle.ne(oracle.shift())][1:]
    for transition_date in transition_dates:
        new_state = int(oracle.loc[transition_date])
        start_position = oracle.index.get_loc(transition_date)
        for name, strategy in positions.items():
            future = strategy.iloc[start_position:]
            matches = future.index[future.eq(new_state)]
            delay = int(oracle.index.get_loc(matches[0]) - start_position) if len(matches) else None
            transition_rows.append({
                "oracle_transition_date": transition_date.date().isoformat(),
                "oracle_new_state": new_state,
                "representative": name,
                "strategy_state_on_transition": int(strategy.loc[transition_date]),
                "first_match_delay_sessions": delay,
            })
    transitions = pd.DataFrame(transition_rows)

    main = metrics.iloc[0]
    diagnosis = "UPSIDE_CAPTURE_DEFICIT" if float(main["positive_capture_deficit_vs_oracle"]) >= float(main["negative_avoidance_deficit_vs_oracle"]) else "DOWNSIDE_AVOIDANCE_DEFICIT"
    selection.to_csv(artifacts / "representative_selection.csv", index=False, encoding="utf-8", lineterminator="\n")
    metrics.to_csv(artifacts / "position_gap_metrics.csv", index=False, encoding="utf-8", lineterminator="\n")
    segments.to_csv(artifacts / "oracle_segment_ledger.csv", index=False, encoding="utf-8", lineterminator="\n")
    transitions.to_csv(artifacts / "transition_gap_ledger.csv", index=False, encoding="utf-8", lineterminator="\n")
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    comparison_positions.rename_axis("Date").to_csv(artifacts / "representative_positions.csv.gz", compression=compression, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "STOP_BEFORE_NEW_MECHANISM_REVIEW_ORACLE_GAPS",
        "main_diagnosis_representative": str(main["representative"]),
        "main_diagnosis": diagnosis,
        "representative_count": len(metrics),
        "oracle_segment_count": int(segment_ids.nunique()),
        "oracle_transition_count": len(transition_dates),
        "oracle_exposure_ratio": float(oracle.mean()),
        "oracle_positive_log_return_capture_ratio": float(open_returns.loc[(open_returns > 0) & oracle.iloc[:-1].astype(bool)].sum() / open_returns.loc[open_returns > 0].sum()),
        "oracle_negative_log_return_avoidance_ratio": float(1.0 - abs(open_returns.loc[(open_returns < 0) & oracle.iloc[:-1].astype(bool)].sum()) / abs(open_returns.loc[open_returns < 0].sum())),
        "reads_sealed_validation": False,
        "search_started": False,
        "new_mechanism_created": False,
        "candidate_created": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "oracle_gap_attribution.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX47 执行记录\n\n"
        f"完成6个原型家族、7个机制代表与EX42日频2次入场Oracle路径的逐日归因。"
        f"共同窗口含{len(oracle_dates)}个交易日、{segment_ids.nunique()}个Oracle状态段和{len(transition_dates)}次状态转换。"
        "未读取封存窗口、启动搜索或形成新机制。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX47 结论\n\n"
        f"机器裁决：`STOP_BEFORE_NEW_MECHANISM_REVIEW_ORACLE_GAPS`。年化最高的因果代表为"
        f"`{main['representative']}`，主差距诊断为`{diagnosis}`。"
        "本结论只定位开发池缺口，下一步须经人工评审后才能提出新的数据或机制假设。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "credential_id": protocol["credential_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": "STOP_BEFORE_NEW_MECHANISM_REVIEW_ORACLE_GAPS",
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
