from __future__ import annotations

from itertools import product
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX41"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(series: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float(
            (np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current))
            / len(valid)
        )

    return series.astype(float).rolling(window, min_periods=minimum).apply(rank_last, raw=True)


def _load_prices(repo: Path, expected_manifest_sha256: str) -> pd.DataFrame:
    manifest_path = repo / "data/raw/518880_execution_manifest.json"
    if _sha256(manifest_path) != expected_manifest_sha256:
        raise ValueError("execution manifest differs from frozen identity")
    manifest = _read(manifest_path)
    frames: list[pd.DataFrame] = []
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
    risk_streak = 0
    cash_age = 0
    targets: list[int] = []
    for row in scores.itertuples():
        risk_event = (
            row.risk_score >= float(rule["exit_score"])
            and row.high_risk_channels >= int(rule["risk_quorum"])
        )
        if state == "LONG":
            risk_streak = risk_streak + 1 if risk_event else 0
            if risk_streak >= int(rule["confirmation_sessions"]):
                state = "CASH"
                cash_age = 0
                risk_streak = 0
        else:
            cash_age += 1
            recovered = (
                cash_age >= int(rule["minimum_cash_sessions"])
                and row.risk_score <= float(rule["reentry_score"])
                and row.price_return_5d > 0.0
            )
            if recovered or cash_age >= int(rule["maximum_cash_sessions"]):
                state = "LONG"
                cash_age = 0
        targets.append(1 if state == "LONG" else 0)
    return pd.Series(targets, index=scores.index, dtype="int8")


def _account(
    targets: pd.Series, prices: pd.DataFrame, fee_rate: float, initial_cash: float, lot_size: int
) -> dict[str, object]:
    start_position = prices.index.get_loc(targets.index[0])
    cash = float(initial_cash)
    quantity = 0
    entries = 0
    closed = 0
    equity_values: list[float] = []
    quantities: list[int] = []
    dates: list[pd.Timestamp] = []
    for position in range(start_position + 1, len(prices)):
        target = int(targets.iloc[position - start_position - 1])
        open_price = float(prices.iloc[position]["open"])
        if target == 1 and quantity == 0:
            purchasable = int(cash / (open_price * (1.0 + fee_rate)) / lot_size) * lot_size
            if purchasable > 0:
                cash -= purchasable * open_price * (1.0 + fee_rate)
                quantity = purchasable
                entries += 1
        elif target == 0 and quantity > 0:
            cash += quantity * open_price * (1.0 - fee_rate)
            quantity = 0
            closed += 1
        equity_values.append(cash + quantity * float(prices.iloc[position]["close"]))
        quantities.append(quantity)
        dates.append(prices.index[position])
    equity = pd.Series(equity_values, index=pd.DatetimeIndex(dates), dtype=float)
    annualized = float((equity.iloc[-1] / initial_cash) ** (252 / len(equity)) - 1.0)
    drawdown = equity.div(equity.cummax().clip(lower=initial_cash)).sub(1.0)
    maximum_drawdown = float(abs(drawdown.min()))
    return {
        "annualized_return": annualized,
        "maximum_drawdown_magnitude": maximum_drawdown,
        "calmar_ratio": annualized / maximum_drawdown if maximum_drawdown > 1e-12 else None,
        "total_return": float(equity.iloc[-1] / initial_cash - 1.0),
        "entry_count": entries,
        "closed_trade_count": closed,
        "exposure_ratio": float(np.mean(np.asarray(quantities) > 0)),
    }


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_sealed_validation", "uses_srt", "uses_txe", "starts_formal_search",
        "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("P04 discovery scope exceeds contract")

    ex40 = repo / "experiments/S008/20260923_S008_EX40"
    ex16 = repo / "experiments/S008/20260923_S008_EX16"
    validate_experiment_archive(ex40)
    validate_experiment_archive(ex16)
    source_paths = {
        "ex40_manifest_sha256": ex40 / "experiment_manifest.json",
        "risk_component_panel_sha256": ex40 / "artifacts/risk_component_panel.csv",
        "feature_panel_sha256": ex16 / "artifacts/causal_feature_panel.csv.gz",
        "execution_manifest_sha256": repo / "data/raw/518880_execution_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")

    eligible = pd.read_csv(source_paths["risk_component_panel_sha256"])
    selected = dict(protocol["risk_components"])
    if not set(selected).issubset(set(eligible["feature"])):
        raise ValueError("P04 selected component is not eligible in EX40")
    panel = pd.read_csv(source_paths["feature_panel_sha256"])
    panel["Date"] = pd.to_datetime(panel["Date"], errors="raise")
    panel = panel.set_index("Date").sort_index()
    prices = _load_prices(repo, protocol["sources"]["execution_manifest_sha256"])
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and execution calendar differ")

    normalization = protocol["normalization"]
    normalized = pd.DataFrame(index=panel.index)
    for feature, orientation in selected.items():
        value = _percentile(
            panel[feature], int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        )
        normalized[feature] = value if int(orientation) > 0 else 1.0 - value
    finite = normalized.notna().all(axis=1)
    evaluation_start = finite.index[finite][0]
    normalized = normalized.loc[evaluation_start:]
    scores = pd.DataFrame(index=normalized.index)
    scores["risk_score"] = normalized.mean(axis=1)
    scores["price_return_5d"] = panel.loc[scores.index, "price_return_5d"]

    grid = dict(protocol["grid"])
    names = list(grid)
    combinations = list(product(*(grid[name] for name in names)))
    if len(combinations) != int(protocol["grid_size"]):
        raise ValueError("P04 grid size differs from protocol")
    execution = protocol["execution"]
    benchmark_targets = pd.Series(1, index=scores.index, dtype="int8")
    benchmark_primary = _account(
        benchmark_targets, prices, float(execution["primary_one_way_cost_bps"]) / 10000.0,
        float(execution["initial_cash"]), int(execution["lot_size"]),
    )
    benchmark_stress = _account(
        benchmark_targets, prices, float(execution["stress_one_way_cost_bps"]) / 10000.0,
        float(execution["initial_cash"]), int(execution["lot_size"]),
    )
    rows: list[dict[str, object]] = []
    for trial_number, values in enumerate(combinations):
        rule = dict(zip(names, values, strict=True))
        channel_threshold = float(rule["channel_threshold"])
        scores["high_risk_channels"] = normalized.ge(channel_threshold).sum(axis=1)
        targets = _targets(scores, rule)
        primary = _account(
            targets, prices, float(execution["primary_one_way_cost_bps"]) / 10000.0,
            float(execution["initial_cash"]), int(execution["lot_size"]),
        )
        stress = _account(
            targets, prices, float(execution["stress_one_way_cost_bps"]) / 10000.0,
            float(execution["initial_cash"]), int(execution["lot_size"]),
        )
        annual_pass = primary["annualized_return"] >= benchmark_primary["annualized_return"] * 1.5
        drawdown_pass = (
            primary["maximum_drawdown_magnitude"]
            < benchmark_primary["maximum_drawdown_magnitude"]
        )
        rows.append({
            "trial_number": trial_number, "parameters_json": json.dumps(rule, sort_keys=True),
            "primary_metrics_json": json.dumps(primary, sort_keys=True),
            "stress_metrics_json": json.dumps(stress, sort_keys=True),
            "annualized_return": primary["annualized_return"],
            "maximum_drawdown_magnitude": primary["maximum_drawdown_magnitude"],
            "calmar_ratio": primary["calmar_ratio"], "entry_count": primary["entry_count"],
            "closed_trade_count": primary["closed_trade_count"],
            "exposure_ratio": primary["exposure_ratio"],
            "annualized_return_gate_pass": annual_pass,
            "maximum_drawdown_gate_pass": drawdown_pass,
            "hard_qualified": bool(annual_pass and drawdown_pass),
            "behavior_sha256": hashlib.sha256(targets.to_numpy(dtype="int8").tobytes()).hexdigest(),
        })

    ledger = pd.DataFrame(rows)
    qualified = ledger.loc[ledger["hard_qualified"]].copy()
    decision = (
        "PROCEED_TO_P04_SRT_IMPLEMENTATION_GATE"
        if len(qualified)
        else "STOP_P04_DISCOVERY_NO_FEASIBLE_REGION"
    )
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.to_csv(
        artifacts / "discovery_grid_ledger.csv.gz", index=False,
        compression=compression, lineterminator="\n",
    )
    qualified.to_csv(
        artifacts / "hard_qualified_discovery.csv", index=False,
        encoding="utf-8", lineterminator="\n",
    )
    evidence = {
        "schema_version": 1, "experiment_id": EXPERIMENT_ID,
        "evidence_scope": "DISCOVERY_ONLY", "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "evaluation_start": evaluation_start.date().isoformat(),
        "evaluation_end": scores.index[-1].date().isoformat(),
        "grid_size": len(ledger), "unique_behaviors": int(ledger["behavior_sha256"].nunique()),
        "hard_qualified_count": int(len(qualified)),
        "unique_hard_qualified_behaviors": int(qualified["behavior_sha256"].nunique()),
        "buyhold_primary": benchmark_primary, "buyhold_stress": benchmark_stress,
        "uses_srt": False, "uses_txe": False, "sealed_validation_read": False,
        "candidate_created": False, "catalog_mutated": False,
        "platform_mutated": False, "pte_mutated": False,
    }
    _write(artifacts / "discovery_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX41 执行记录\n\n"
        f"完成P04共{len(ledger)}组冻结发现性网格，形成{ledger['behavior_sha256'].nunique()}种"
        f"独立目标仓位行为，主成本两项硬门同时通过{len(qualified)}组。"
        "本实验使用近似账户执行器，未使用SRT或TXE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX41 实验结论\n\n"
        f"机器裁决：`{decision}`。发现性硬门通过{len(qualified)}组、"
        f"{qualified['behavior_sha256'].nunique()}种独立行为。"
        "即使通过也只能进入正式SRT实现门，不能创建候选或读取封存池。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID, "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"], "strategy_id": "S008",
            "credential_id": "SGC-S008-001", "symbol": "518880.SH",
            "development_cutoff": "2024-12-31", "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
