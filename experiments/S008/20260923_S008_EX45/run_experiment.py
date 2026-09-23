from __future__ import annotations

from itertools import product
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX45"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(series: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float((np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current)) / len(valid))

    return series.astype(float).rolling(window, min_periods=minimum).apply(rank_last, raw=True)


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


def _targets(scores: pd.DataFrame, prototype: str, rule: dict[str, object]) -> pd.Series:
    state = "CASH"
    entry_streak = 0
    exit_streak = 0
    cadence = int(rule["evaluation_cadence_sessions"])
    confirmation = int(rule["confirmation_evaluations"])
    targets: list[int] = []
    for position, row in enumerate(scores.itertuples()):
        if position % cadence == 0:
            threshold = float(rule["equity_channel_threshold"])
            support_count = int(sum(value >= threshold for value in (row.equity_return, row.equity_drawdown, row.equity_turnover)))
            if prototype == "TRANSLATION_LED":
                entry_event = row.currency >= float(rule["currency_entry"]) and support_count >= int(rule["entry_equity_quorum"])
                exit_event = row.currency <= float(rule["currency_exit"]) and support_count <= int(rule["exit_equity_quorum"])
            elif prototype == "RISK_OFF_LED":
                boosted_score = min(1.0, row.equity_score + float(rule["currency_boost_weight"]) * max(row.currency - 0.5, 0.0))
                entry_event = boosted_score >= float(rule["equity_entry_score"]) and support_count >= int(rule["entry_equity_quorum"])
                exit_event = row.equity_score <= float(rule["equity_exit_score"]) and support_count <= int(rule["exit_equity_quorum"])
            else:
                raise ValueError(f"unknown prototype: {prototype}")
            if state == "CASH":
                entry_streak = entry_streak + 1 if entry_event else 0
                exit_streak = 0
                if entry_streak >= confirmation:
                    state = "LONG"
                    entry_streak = 0
            else:
                exit_streak = exit_streak + 1 if exit_event else 0
                entry_streak = 0
                if exit_streak >= confirmation:
                    state = "CASH"
                    exit_streak = 0
        targets.append(1 if state == "LONG" else 0)
    return pd.Series(targets, index=scores.index, dtype="int8")


def _account(targets: pd.Series, prices: pd.DataFrame, fee_rate: float, initial_cash: float, lot_size: int) -> dict[str, object]:
    start_position = prices.index.get_loc(targets.index[0])
    cash = float(initial_cash)
    quantity = 0
    entry_cost = 0.0
    trade_returns: list[float] = []
    entries = 0
    equity_values: list[float] = []
    quantities: list[int] = []
    dates: list[pd.Timestamp] = []
    for position in range(start_position + 1, len(prices)):
        target = int(targets.iloc[position - start_position - 1])
        open_price = float(prices.iloc[position]["open"])
        if target == 1 and quantity == 0:
            purchasable = int(cash / (open_price * (1.0 + fee_rate)) / lot_size) * lot_size
            if purchasable > 0:
                entry_cost = purchasable * open_price * (1.0 + fee_rate)
                cash -= entry_cost
                quantity = purchasable
                entries += 1
        elif target == 0 and quantity > 0:
            proceeds = quantity * open_price * (1.0 - fee_rate)
            trade_returns.append(proceeds / entry_cost - 1.0)
            cash += proceeds
            quantity = 0
            entry_cost = 0.0
        equity_values.append(cash + quantity * float(prices.iloc[position]["close"]))
        quantities.append(quantity)
        dates.append(prices.index[position])
    if quantity > 0 and entry_cost > 0:
        trade_returns.append(quantity * float(prices.iloc[-1]["close"]) / entry_cost - 1.0)
    equity = pd.Series(equity_values, index=pd.DatetimeIndex(dates), dtype=float)
    annualized = float((equity.iloc[-1] / initial_cash) ** (252 / len(equity)) - 1.0)
    drawdown = equity.div(equity.cummax().clip(lower=initial_cash)).sub(1.0)
    maximum_drawdown = float(abs(drawdown.min()))
    profits = sum(value for value in trade_returns if value > 0)
    losses = abs(sum(value for value in trade_returns if value < 0))
    return {
        "annualized_return": annualized,
        "maximum_drawdown_magnitude": maximum_drawdown,
        "calmar_ratio": annualized / maximum_drawdown if maximum_drawdown > 1e-12 else None,
        "profit_loss_ratio": profits / losses if losses > 1e-12 else None,
        "total_return": float(equity.iloc[-1] / initial_cash - 1.0),
        "entry_count": entries,
        "closed_or_marked_trade_count": len(trade_returns),
        "exposure_ratio": float(np.mean(np.asarray(quantities) > 0)),
    }


def _grid(protocol: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    common = dict(protocol["common_grid"])
    rows: list[tuple[str, dict[str, object]]] = []
    for prototype, key in (("TRANSLATION_LED", "translation_led_grid"), ("RISK_OFF_LED", "risk_off_led_grid")):
        grid = {**common, **dict(protocol[key])}
        names = list(grid)
        rows.extend((prototype, dict(zip(names, values, strict=True))) for values in product(*(grid[name] for name in names)))
    return rows


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = ("reads_sealed_validation", "uses_srt", "uses_txe", "starts_formal_search", "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_platform", "mutates_pte")
    if not protocol.get("reads_development_returns") or any(protocol.get(key) for key in prohibited):
        raise ValueError("P05 discovery scope exceeds contract")

    ex44 = repo / "experiments/S008/20260923_S008_EX44"
    ex16 = repo / "experiments/S008/20260923_S008_EX16"
    validate_experiment_archive(ex44)
    validate_experiment_archive(ex16)
    source_paths = {
        "ex44_manifest_sha256": ex44 / "experiment_manifest.json",
        "component_panel_sha256": ex44 / "artifacts/long_cycle_component_panel.csv",
        "feature_panel_sha256": ex16 / "artifacts/causal_feature_panel.csv.gz",
        "execution_manifest_sha256": repo / "data/raw/518880_execution_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")
    eligible = pd.read_csv(source_paths["component_panel_sha256"])
    selected = dict(protocol["orientations"])
    if set(selected) != set(eligible["feature"]):
        raise ValueError("P05 component identity differs from EX44")

    panel = pd.read_csv(source_paths["feature_panel_sha256"])
    panel["Date"] = pd.to_datetime(panel.pop("Date"), errors="raise")
    panel = panel.set_index("Date").sort_index()
    prices = _load_prices(repo, protocol["sources"]["execution_manifest_sha256"])
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and execution calendar differ")
    normalization = protocol["normalization"]
    normalized = pd.DataFrame(index=panel.index)
    for feature, orientation in selected.items():
        value = _percentile(panel[feature], int(normalization["lookback_sessions"]), int(normalization["minimum_observations"]))
        normalized[feature] = value if int(orientation) > 0 else 1.0 - value
    finite = normalized.notna().all(axis=1)
    evaluation_start = finite.index[finite][0]
    normalized = normalized.loc[evaluation_start:]
    equity_features = list(protocol["components"]["equity"])
    scores = pd.DataFrame(index=normalized.index)
    scores["currency"] = normalized[str(protocol["components"]["currency"])]
    scores["equity_return"] = normalized[equity_features[0]]
    scores["equity_drawdown"] = normalized[equity_features[1]]
    scores["equity_turnover"] = normalized[equity_features[2]]
    scores["equity_score"] = normalized[equity_features].mean(axis=1)

    combinations = _grid(protocol)
    if len(combinations) != int(protocol["grid_size"]):
        raise ValueError(f"P05 grid size differs: {len(combinations)}")
    counts = pd.Series([prototype for prototype, _ in combinations]).value_counts()
    if counts["TRANSLATION_LED"] != int(protocol["translation_led_grid_size"]) or counts["RISK_OFF_LED"] != int(protocol["risk_off_led_grid_size"]):
        raise ValueError("P05 prototype grid sizes differ")
    execution = protocol["execution"]
    benchmark_targets = pd.Series(1, index=scores.index, dtype="int8")
    benchmark_primary = _account(benchmark_targets, prices, float(execution["primary_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
    benchmark_stress = _account(benchmark_targets, prices, float(execution["stress_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
    rows: list[dict[str, object]] = []
    for trial_number, (prototype, rule) in enumerate(combinations):
        targets = _targets(scores, prototype, rule)
        primary = _account(targets, prices, float(execution["primary_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
        stress = _account(targets, prices, float(execution["stress_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
        annual_pass = primary["annualized_return"] >= benchmark_primary["annualized_return"] * 1.5
        drawdown_pass = primary["maximum_drawdown_magnitude"] < benchmark_primary["maximum_drawdown_magnitude"]
        rows.append({
            "trial_number": trial_number,
            "prototype": prototype,
            "parameters_json": json.dumps(rule, sort_keys=True),
            "primary_metrics_json": json.dumps(primary, sort_keys=True),
            "stress_metrics_json": json.dumps(stress, sort_keys=True),
            "annualized_return": primary["annualized_return"],
            "maximum_drawdown_magnitude": primary["maximum_drawdown_magnitude"],
            "calmar_ratio": primary["calmar_ratio"],
            "profit_loss_ratio": primary["profit_loss_ratio"],
            "entry_count": primary["entry_count"],
            "exposure_ratio": primary["exposure_ratio"],
            "stress_annualized_return": stress["annualized_return"],
            "stress_maximum_drawdown_magnitude": stress["maximum_drawdown_magnitude"],
            "annualized_return_gate_pass": annual_pass,
            "maximum_drawdown_gate_pass": drawdown_pass,
            "hard_qualified": bool(annual_pass and drawdown_pass),
            "behavior_sha256": hashlib.sha256(targets.to_numpy(dtype="int8").tobytes()).hexdigest(),
        })

    ledger = pd.DataFrame(rows)
    qualified = ledger.loc[ledger["hard_qualified"]].copy()
    decision = "PROCEED_TO_P05_STABILITY_REVIEW" if len(qualified) else "STOP_P05_DISCOVERY_NO_FEASIBLE_REGION"
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.to_csv(artifacts / "discovery_grid_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    qualified.to_csv(artifacts / "hard_qualified_discovery.csv", index=False, encoding="utf-8", lineterminator="\n")
    summary = ledger.groupby("prototype", as_index=False).agg(
        trials=("trial_number", "size"),
        unique_behaviors=("behavior_sha256", "nunique"),
        hard_qualified=("hard_qualified", "sum"),
        best_annualized_return=("annualized_return", "max"),
        lowest_maximum_drawdown=("maximum_drawdown_magnitude", "min"),
        median_annualized_return=("annualized_return", "median"),
        median_maximum_drawdown=("maximum_drawdown_magnitude", "median"),
    )
    summary.to_csv(artifacts / "prototype_summary.csv", index=False, encoding="utf-8", lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "evidence_scope": "DISCOVERY_ONLY",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "evaluation_start": evaluation_start.date().isoformat(),
        "evaluation_end": scores.index[-1].date().isoformat(),
        "grid_size": len(ledger),
        "unique_behaviors": int(ledger["behavior_sha256"].nunique()),
        "hard_qualified_count": int(len(qualified)),
        "unique_hard_qualified_behaviors": int(qualified["behavior_sha256"].nunique()),
        "qualified_by_prototype": {str(key): int(value) for key, value in qualified.groupby("prototype").size().items()},
        "buyhold_primary": benchmark_primary,
        "buyhold_stress": benchmark_stress,
        "uses_srt": False,
        "uses_txe": False,
        "sealed_validation_read": False,
        "candidate_created": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False
    }
    _write(artifacts / "discovery_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX45 执行记录\n\n"
        f"完成P05两类状态机共{len(ledger)}组冻结发现性网格，形成{ledger['behavior_sha256'].nunique()}种独立目标仓位行为，"
        f"主成本两项硬门同时通过{len(qualified)}组。实验使用近似账户执行器，未使用SRT或TXE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX45 结论\n\n"
        f"机器裁决：`{decision}`。发现性硬门通过{len(qualified)}组、{qualified['behavior_sha256'].nunique()}种独立行为。"
        "即使通过也只能进入稳定性审查，不能选择最终参数、创建候选或读取封存窗口。\n",
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
        "decision": decision,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
