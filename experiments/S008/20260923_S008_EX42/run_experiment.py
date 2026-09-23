from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX42"


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


def _solve_oracle(
    opens: np.ndarray,
    final_close: float,
    fee_rate: float,
    maximum_entries: int,
    cadence_sessions: int,
) -> tuple[np.ndarray, int, str, float]:
    count = len(opens)
    negative = -np.inf
    cash = np.full((count, maximum_entries + 1), negative, dtype=float)
    hold = np.full((count, maximum_entries + 1), negative, dtype=float)
    cash_action = np.full((count, maximum_entries + 1), -1, dtype=np.int8)
    hold_action = np.full((count, maximum_entries + 1), -1, dtype=np.int8)
    cash[0, 0] = 1_000_000.0
    cash_action[0, 0] = 0
    hold[0, 1] = 1_000_000.0 / (opens[0] * (1.0 + fee_rate))
    hold_action[0, 1] = 1

    for position in range(1, count):
        can_trade = position % cadence_sessions == 0
        for entries in range(maximum_entries + 1):
            keep_cash = cash[position - 1, entries]
            sell = (
                hold[position - 1, entries] * opens[position] * (1.0 - fee_rate)
                if can_trade
                else negative
            )
            if keep_cash >= sell:
                cash[position, entries] = keep_cash
                cash_action[position, entries] = 0
            else:
                cash[position, entries] = sell
                cash_action[position, entries] = 1

            keep_hold = hold[position - 1, entries]
            buy = (
                cash[position - 1, entries - 1] / (opens[position] * (1.0 + fee_rate))
                if can_trade and entries > 0
                else negative
            )
            if keep_hold >= buy:
                hold[position, entries] = keep_hold
                hold_action[position, entries] = 0
            else:
                hold[position, entries] = buy
                hold_action[position, entries] = 1

    terminal: list[tuple[float, int, str]] = []
    for entries in range(maximum_entries + 1):
        terminal.append((cash[-1, entries], entries, "CASH"))
        terminal.append((hold[-1, entries] * final_close, entries, "LONG"))
    wealth, entries, state = max(terminal, key=lambda item: item[0])
    positions = np.zeros(count, dtype=np.int8)
    current_entries = entries
    current_state = state
    for position in range(count - 1, -1, -1):
        positions[position] = 1 if current_state == "LONG" else 0
        if position == 0:
            break
        if current_state == "CASH":
            action = cash_action[position, current_entries]
            current_state = "LONG" if action == 1 else "CASH"
        else:
            action = hold_action[position, current_entries]
            if action == 1:
                current_state = "CASH"
                current_entries -= 1
            else:
                current_state = "LONG"
    return positions, entries, state, float(wealth)


def _account(
    positions: np.ndarray,
    prices: pd.DataFrame,
    fee_rate: float,
    initial_cash: float,
) -> tuple[dict[str, object], pd.Series]:
    cash = float(initial_cash)
    shares = 0.0
    entries = 0
    exits = 0
    equity_values: list[float] = []
    for offset, target in enumerate(positions):
        open_price = float(prices.iloc[offset]["open"])
        if target == 1 and shares == 0.0:
            shares = cash / (open_price * (1.0 + fee_rate))
            cash = 0.0
            entries += 1
        elif target == 0 and shares > 0.0:
            cash = shares * open_price * (1.0 - fee_rate)
            shares = 0.0
            exits += 1
        equity_values.append(cash + shares * float(prices.iloc[offset]["close"]))
    equity = pd.Series(equity_values, index=prices.index, dtype=float)
    annualized = float((equity.iloc[-1] / initial_cash) ** (252 / len(equity)) - 1.0)
    drawdown = equity.div(equity.cummax().clip(lower=initial_cash)).sub(1.0)
    maximum_drawdown = float(abs(drawdown.min()))
    metrics = {
        "annualized_return": annualized,
        "maximum_drawdown_magnitude": maximum_drawdown,
        "calmar_ratio": annualized / maximum_drawdown if maximum_drawdown > 1e-12 else None,
        "total_return": float(equity.iloc[-1] / initial_cash - 1.0),
        "entry_count": entries,
        "exit_count": exits,
        "exposure_ratio": float(np.mean(positions)),
    }
    return metrics, equity


def _alpha_budget(positions: np.ndarray, opens: np.ndarray) -> dict[str, float]:
    returns = np.log(opens[1:] / opens[:-1])
    held = positions[:-1].astype(bool)
    positive = returns > 0
    negative = returns < 0
    positive_total = float(returns[positive].sum())
    negative_total = float(abs(returns[negative].sum()))
    return {
        "positive_log_return_capture_ratio": (
            float(returns[positive & held].sum()) / positive_total if positive_total > 0 else 0.0
        ),
        "negative_log_return_avoidance_ratio": (
            1.0 - float(abs(returns[negative & held].sum())) / negative_total
            if negative_total > 0
            else 0.0
        ),
    }


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("perfect_foresight"):
        raise ValueError("oracle protocol identity is invalid")
    prohibited = (
        "reads_sealed_validation", "candidate_generation", "promotion_allowed",
        "mutates_catalog", "mutates_platform", "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("oracle scope exceeds discovery")

    ex38 = repo / "experiments/S008/20260923_S008_EX38"
    ex41 = repo / "experiments/S008/20260923_S008_EX41"
    validate_experiment_archive(ex38)
    validate_experiment_archive(ex41)
    source_paths = {
        "ex38_manifest_sha256": ex38 / "experiment_manifest.json",
        "ex38_search_evidence_sha256": ex38 / "artifacts/search_evidence.json",
        "ex41_manifest_sha256": ex41 / "experiment_manifest.json",
        "execution_manifest_sha256": repo / "data/raw/518880_execution_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")

    prices = _load_prices(repo, protocol["sources"]["execution_manifest_sha256"])
    prices = prices.loc[
        prices.index.to_series().gt(protocol["evaluation_start"])
        & prices.index.to_series().le(protocol["evaluation_end"])
    ].copy()
    execution = protocol["execution"]
    initial_cash = float(execution["initial_cash"])
    primary_fee = float(execution["primary_one_way_cost_bps"]) / 10000.0
    stress_fee = float(execution["stress_one_way_cost_bps"]) / 10000.0
    benchmark_positions = np.ones(len(prices), dtype=np.int8)
    benchmark_primary, _ = _account(benchmark_positions, prices, primary_fee, initial_cash)
    benchmark_stress, _ = _account(benchmark_positions, prices, stress_fee, initial_cash)
    return_gate = float(benchmark_primary["annualized_return"]) * 1.5
    drawdown_gate = float(benchmark_primary["maximum_drawdown_magnitude"])

    rows: list[dict[str, object]] = []
    path_rows: list[pd.DataFrame] = []
    opens = prices["open"].to_numpy(dtype=float)
    for cadence_name, cadence_sessions in protocol["decision_cadences"].items():
        maximum_budget = max(int(item) for item in protocol["maximum_entry_budgets"])
        solved_positions, _, _, _ = _solve_oracle(
            opens, float(prices.iloc[-1]["close"]), primary_fee,
            maximum_budget, int(cadence_sessions),
        )
        del solved_positions
        for budget in protocol["maximum_entry_budgets"]:
            positions, dp_entries, terminal_state, terminal_wealth = _solve_oracle(
                opens, float(prices.iloc[-1]["close"]), primary_fee,
                int(budget), int(cadence_sessions),
            )
            primary, equity = _account(positions, prices, primary_fee, initial_cash)
            stress, _ = _account(positions, prices, stress_fee, initial_cash)
            alpha = _alpha_budget(positions, opens)
            annual_pass = float(primary["annualized_return"]) >= return_gate
            drawdown_pass = float(primary["maximum_drawdown_magnitude"]) < drawdown_gate
            rows.append({
                "cadence": cadence_name, "cadence_sessions": int(cadence_sessions),
                "maximum_entry_budget": int(budget), "dp_selected_entries": int(dp_entries),
                "terminal_state": terminal_state, "dp_terminal_wealth": terminal_wealth,
                "primary_metrics_json": json.dumps(primary, sort_keys=True),
                "stress_metrics_json": json.dumps(stress, sort_keys=True),
                "annualized_return": primary["annualized_return"],
                "maximum_drawdown_magnitude": primary["maximum_drawdown_magnitude"],
                "calmar_ratio": primary["calmar_ratio"], "exposure_ratio": primary["exposure_ratio"],
                "entry_count": primary["entry_count"], "exit_count": primary["exit_count"],
                **alpha, "annualized_return_gate_pass": annual_pass,
                "maximum_drawdown_gate_pass": drawdown_pass,
                "hard_qualified": bool(annual_pass and drawdown_pass),
                "behavior_sha256": hashlib.sha256(positions.tobytes()).hexdigest(),
            })
            path_rows.append(pd.DataFrame({
                "Date": prices.index, "cadence": cadence_name,
                "maximum_entry_budget": int(budget), "target_position": positions,
                "primary_equity": equity.to_numpy(dtype=float),
            }))

    ledger = pd.DataFrame(rows)
    qualified = ledger.loc[ledger["hard_qualified"]].copy()
    decision = (
        "TARGET_FEASIBLE_UNDER_ORACLE"
        if len(qualified)
        else "TARGET_NOT_REACHED_BY_PREREGISTERED_ORACLE"
    )
    ledger.to_csv(
        artifacts / "oracle_budget_ledger.csv", index=False,
        encoding="utf-8", lineterminator="\n",
    )
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    pd.concat(path_rows, ignore_index=True).to_csv(
        artifacts / "oracle_paths.csv.gz", index=False,
        compression=compression, lineterminator="\n",
    )
    minimum_qualified: dict[str, int | None] = {}
    for cadence_name in protocol["decision_cadences"]:
        subset = qualified.loc[qualified["cadence"].eq(cadence_name)]
        minimum_qualified[cadence_name] = (
            int(subset["maximum_entry_budget"].min()) if len(subset) else None
        )
    evidence = {
        "schema_version": 1, "experiment_id": EXPERIMENT_ID,
        "evidence_scope": "DISCOVERY_ONLY_ORACLE", "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "evaluation_start": prices.index[0].date().isoformat(),
        "evaluation_end": prices.index[-1].date().isoformat(),
        "scenario_count": int(len(ledger)), "hard_qualified_scenario_count": int(len(qualified)),
        "minimum_qualified_entry_budget_by_cadence": minimum_qualified,
        "buyhold_primary": benchmark_primary, "buyhold_stress": benchmark_stress,
        "annualized_return_gate": return_gate, "maximum_drawdown_gate": drawdown_gate,
        "perfect_foresight": True, "continuous_shares": True,
        "sealed_validation_read": False, "candidate_created": False,
        "catalog_mutated": False, "platform_mutated": False, "pte_mutated": False,
    }
    _write(artifacts / "oracle_feasibility.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX42 执行记录\n\n"
        f"完成{len(ledger)}个完美事后信息场景，覆盖三种决策频率和十档入场预算；"
        f"同时满足两项硬门的场景为{len(qualified)}个。路径只作为理论上界。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX42 实验结论\n\n"
        f"机器裁决：`{decision}`。各决策频率首次同时满足两项硬门的最大入场预算为"
        f"{minimum_qualified}。该结果使用完美事后信息和连续份额，只说明数学可达性。\n",
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
