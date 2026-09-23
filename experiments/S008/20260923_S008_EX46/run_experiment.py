from __future__ import annotations

from itertools import product
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX46"


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


def _targets(scores: pd.DataFrame, rule: dict[str, object]) -> pd.Series:
    state = "LONG"
    exit_streak = 0
    entry_streak = 0
    cash_age = 0
    cadence = int(rule["evaluation_cadence_sessions"])
    confirmation = int(rule["confirmation_evaluations"])
    targets: list[int] = []
    for position, row in enumerate(scores.itertuples()):
        if state == "CASH":
            cash_age += 1
        if position % cadence == 0:
            exit_event = (
                row.regime_score <= float(rule["exit_regime_score"])
                and row.price_return_60d <= float(rule["exit_price_return_60d"])
                and row.price_trend_distance_60 < 0.0
            )
            reentry_event = (
                row.regime_score >= float(rule["reentry_regime_score"])
                or (
                    row.price_return_20d >= float(rule["reentry_price_return_20d"])
                    and row.price_trend_distance_60 > 0.0
                )
                or cash_age >= int(rule["maximum_cash_sessions"])
            )
            if state == "LONG":
                exit_streak = exit_streak + 1 if exit_event else 0
                entry_streak = 0
                if exit_streak >= confirmation:
                    state = "CASH"
                    exit_streak = 0
                    cash_age = 0
            else:
                entry_streak = entry_streak + 1 if reentry_event else 0
                exit_streak = 0
                if entry_streak >= confirmation:
                    state = "LONG"
                    entry_streak = 0
                    cash_age = 0
        targets.append(1 if state == "LONG" else 0)
    return pd.Series(targets, index=scores.index, dtype="int8")


def _account(targets: pd.Series, prices: pd.DataFrame, fee_rate: float, initial_cash: float, lot_size: int) -> dict[str, object]:
    start = prices.index.get_loc(targets.index[0])
    opens = prices["open"].to_numpy(dtype=float)[start + 1 :]
    closes = prices["close"].to_numpy(dtype=float)[start + 1 :]
    desired = targets.to_numpy(dtype=np.int8)[: len(opens)]
    cash = float(initial_cash)
    quantity = 0
    entry_cost = 0.0
    entries = 0
    trade_returns: list[float] = []
    equity = np.empty(len(opens), dtype=float)
    held = np.empty(len(opens), dtype=bool)
    for index, (target, open_price, close_price) in enumerate(zip(desired, opens, closes, strict=True)):
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
        equity[index] = cash + quantity * close_price
        held[index] = quantity > 0
    if quantity > 0 and entry_cost > 0:
        trade_returns.append(quantity * closes[-1] / entry_cost - 1.0)
    running_peak = np.maximum.accumulate(np.maximum(equity, initial_cash))
    maximum_drawdown = float(abs(np.min(equity / running_peak - 1.0)))
    annualized = float((equity[-1] / initial_cash) ** (252 / len(equity)) - 1.0)
    profits = sum(value for value in trade_returns if value > 0)
    losses = abs(sum(value for value in trade_returns if value < 0))
    return {
        "annualized_return": annualized,
        "maximum_drawdown_magnitude": maximum_drawdown,
        "calmar_ratio": annualized / maximum_drawdown if maximum_drawdown > 1e-12 else None,
        "profit_loss_ratio": profits / losses if losses > 1e-12 else None,
        "total_return": float(equity[-1] / initial_cash - 1.0),
        "entry_count": entries,
        "closed_or_marked_trade_count": len(trade_returns),
        "exposure_ratio": float(held.mean()),
    }


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = ("reads_sealed_validation", "uses_srt", "uses_txe", "starts_formal_search", "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_platform", "mutates_pte")
    if not protocol.get("reads_development_returns") or any(protocol.get(key) for key in prohibited):
        raise ValueError("P06 discovery scope exceeds contract")

    ex45 = repo / "experiments/S008/20260923_S008_EX45"
    ex16 = repo / "experiments/S008/20260923_S008_EX16"
    validate_experiment_archive(ex45)
    validate_experiment_archive(ex16)
    source_paths = {
        "ex45_manifest_sha256": ex45 / "experiment_manifest.json",
        "ex45_ledger_sha256": ex45 / "artifacts/discovery_grid_ledger.csv.gz",
        "feature_panel_sha256": ex16 / "artifacts/causal_feature_panel.csv.gz",
        "execution_manifest_sha256": repo / "data/raw/518880_execution_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")
    selected = dict(protocol["favorable_components"])
    price_features = list(protocol["price_components"])
    raw = pd.read_csv(source_paths["feature_panel_sha256"])
    dates = pd.to_datetime(raw.pop("Date"), errors="raise")
    panel = raw[list(selected) + price_features].copy()
    panel.index = dates
    panel = panel.sort_index()
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
    equity_features = [name for name in selected if name != "currency_usdcnh_return_20d"]

    grid = dict(protocol["grid"])
    names = list(grid)
    combinations = [dict(zip(names, values, strict=True)) for values in product(*(grid[name] for name in names))]
    if len(combinations) != int(protocol["grid_size"]):
        raise ValueError(f"P06 grid size differs: {len(combinations)}")
    execution = protocol["execution"]
    benchmark_targets = pd.Series(1, index=normalized.index, dtype="int8")
    benchmark_primary = _account(benchmark_targets, prices, float(execution["primary_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
    benchmark_stress = _account(benchmark_targets, prices, float(execution["stress_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
    rows: list[dict[str, object]] = []
    for trial_number, rule in enumerate(combinations):
        weight = float(rule["currency_weight"])
        scores = pd.DataFrame(index=normalized.index)
        scores["regime_score"] = weight * normalized["currency_usdcnh_return_20d"] + (1.0 - weight) * normalized[equity_features].mean(axis=1)
        for feature in price_features:
            scores[feature] = panel.loc[scores.index, feature]
        targets = _targets(scores, rule)
        primary = _account(targets, prices, float(execution["primary_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
        stress = _account(targets, prices, float(execution["stress_one_way_cost_bps"]) / 10000.0, float(execution["initial_cash"]), int(execution["lot_size"]))
        annual_pass = primary["annualized_return"] >= benchmark_primary["annualized_return"] * 1.5
        drawdown_pass = primary["maximum_drawdown_magnitude"] < benchmark_primary["maximum_drawdown_magnitude"]
        rows.append({
            "trial_number": trial_number,
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
    decision = "PROCEED_TO_P06_STABILITY_REVIEW" if len(qualified) else "STOP_P06_DISCOVERY_NO_FEASIBLE_REGION"
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.to_csv(artifacts / "discovery_grid_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    qualified.to_csv(artifacts / "hard_qualified_discovery.csv", index=False, encoding="utf-8", lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "evidence_scope": "DISCOVERY_ONLY",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "evaluation_start": evaluation_start.date().isoformat(),
        "evaluation_end": normalized.index[-1].date().isoformat(),
        "grid_size": len(ledger),
        "unique_behaviors": int(ledger["behavior_sha256"].nunique()),
        "hard_qualified_count": int(len(qualified)),
        "unique_hard_qualified_behaviors": int(qualified["behavior_sha256"].nunique()),
        "annualized_return_gate_pass_count": int(ledger["annualized_return_gate_pass"].sum()),
        "maximum_drawdown_gate_pass_count": int(ledger["maximum_drawdown_gate_pass"].sum()),
        "buyhold_primary": benchmark_primary,
        "buyhold_stress": benchmark_stress,
        "uses_srt": False,
        "uses_txe": False,
        "sealed_validation_read": False,
        "candidate_created": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "discovery_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX46 执行记录\n\n"
        f"完成P06共{len(ledger)}组冻结发现性网格，形成{ledger['behavior_sha256'].nunique()}种独立目标仓位行为，"
        f"主成本两项硬门同时通过{len(qualified)}组。未使用SRT或TXE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX46 结论\n\n"
        f"机器裁决：`{decision}`。年化收益门通过{int(ledger['annualized_return_gate_pass'].sum())}组，"
        f"最大回撤门通过{int(ledger['maximum_drawdown_gate_pass'].sum())}组，两项同时通过{len(qualified)}组。"
        "本实验是本批授权内第三套机制；无论成败均停止并进入人工评审。\n",
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
