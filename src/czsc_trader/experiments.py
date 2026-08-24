"""Small-team champion-challenger experiments with a hard pre-2026 cutoff."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .audit import audit_no_lookahead
from .backtest import run_period_backtests
from .baselines import resolve_baseline
from .data import load_market_data
from .factors import generate_factor_frame
from .objectives import TARGET_PERIODS
from .rules import FACTOR_COLUMNS, Rule, build_factor_events


RESEARCH_CUTOFF = pd.Timestamp("2025-12-31")
VALIDATION_PERIODS = {
    "2023": (pd.Timestamp("2023-01-01"), pd.Timestamp("2023-12-31")),
    "2024": (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")),
    "2025": (pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")),
}
COOLDOWN_CANDIDATES = (0, 2, 3, 5, 8)
REENTRY_GATES = ("none", "structure", "trend", "structure_and_trend")


@dataclass(frozen=True)
class ReentryChallenger:
    cooldown_days: int
    reentry_gate: str

    @property
    def candidate_id(self) -> str:
        return f"cooldown{self.cooldown_days}_gate-{self.reentry_gate}"

    @property
    def complexity(self) -> int:
        return self.cooldown_days + (0 if self.reentry_gate == "none" else 1)


def build_reentry_candidates() -> tuple[ReentryChallenger, ...]:
    return tuple(
        ReentryChallenger(cooldown, gate)
        for cooldown in COOLDOWN_CANDIDATES
        for gate in REENTRY_GATES
    )


def _gate_passes(values: np.ndarray, gate: str) -> np.ndarray:
    gates = {
        "none": np.ones(len(values), dtype=bool),
        "structure": values[:, 0] >= 0.0,
        "trend": values[:, 1] >= 0.0,
        "structure_and_trend": (values[:, 0] >= 0.0) & (values[:, 1] >= 0.0),
    }
    if gate not in gates:
        raise ValueError(f"unknown reentry gate: {gate}")
    return gates[gate]


def positions_with_reentry(
    factors: pd.DataFrame,
    champion: Rule,
    challenger: ReentryChallenger,
) -> tuple[pd.Series, pd.Series]:
    """Apply the champion state machine plus one post-exit re-entry constraint."""
    if challenger.cooldown_days < 0:
        raise ValueError("cooldown days must be non-negative")
    clean = factors.loc[:, FACTOR_COLUMNS].fillna(0.0).astype(float)
    values = clean.to_numpy(dtype=float)
    scores_array = values @ np.asarray(champion.weights, dtype=float)
    champion_gate = _gate_passes(values, champion.entry_gate)
    reentry_gate = _gate_passes(values, challenger.reentry_gate)

    position = 0.0
    confirmations = 0
    exit_confirmations = 0
    holding_days = 0
    cooldown_remaining = 0
    has_exited = False
    positions: list[float] = []
    for i, score in enumerate(scores_array):
        if position == 0.0:
            if cooldown_remaining > 0:
                cooldown_remaining -= 1
                confirmations = 0
                positions.append(position)
                continue
            gate_ok = bool(champion_gate[i]) and (
                not has_exited or bool(reentry_gate[i])
            )
            confirmations = confirmations + 1 if score >= champion.enter and gate_ok else 0
            if confirmations >= champion.confirm_days:
                position = 1.0
                holding_days = 1
                confirmations = 0
                exit_confirmations = 0
        else:
            eligible_exit = holding_days >= champion.min_hold_days
            exit_confirmations = (
                exit_confirmations + 1
                if eligible_exit and score <= champion.exit
                else 0
            )
            if exit_confirmations >= champion.exit_confirm_days:
                position = 0.0
                holding_days = 0
                confirmations = 0
                exit_confirmations = 0
                cooldown_remaining = challenger.cooldown_days
                has_exited = True
            else:
                holding_days += 1
        positions.append(position)
    return (
        pd.Series(positions, index=clean.index, name="target_position", dtype=float),
        pd.Series(scores_array, index=clean.index, name="factor_score", dtype=float),
    )


def window_passes(
    champion_metrics: dict[str, object], challenger_metrics: dict[str, object]
) -> bool:
    return (
        float(challenger_metrics["strategy_return"])
        > float(champion_metrics["strategy_return"])
        and float(challenger_metrics["sharpe"]) > float(champion_metrics["sharpe"])
    )


def all_windows_pass(
    champion: dict[str, dict[str, object]],
    challenger: dict[str, dict[str, object]],
) -> bool:
    return set(champion) == set(challenger) and all(
        window_passes(champion[name], challenger[name]) for name in champion
    )


def rank_challengers(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.sort_values(
        [
            "pass_count",
            "min_return_delta",
            "min_sharpe_delta",
            "mean_return_delta",
            "mean_sharpe_delta",
            "complexity",
            "candidate_id",
        ],
        ascending=[False, False, False, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def _metrics(results: dict[str, object]) -> dict[str, dict[str, object]]:
    return {name: result.metrics for name, result in results.items()}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_pre2026_experiment(
    raw_dir: Path,
    baseline_root: Path,
    output_dir: Path,
    *,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Select at most one challenger without opening a 2026 K-line file."""
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"experiment output is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = resolve_baseline(Path(baseline_root), "baseline_20260823")
    data = load_market_data(
        Path(raw_dir), "588080.SH", "etf", cutoff=RESEARCH_CUTOFF
    )
    factor_result = generate_factor_frame(data)
    factors = factor_result.frame.loc[:, FACTOR_COLUMNS]
    champion_target, champion_scores = positions_with_reentry(
        factors, baseline.rule, ReentryChallenger(0, "none")
    )
    champion_results = run_period_backtests(
        data.daily,
        champion_target,
        VALIDATION_PERIODS,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    champion_metrics = _metrics(champion_results)

    rows: list[dict[str, object]] = []
    candidate_payloads: dict[str, tuple[ReentryChallenger, pd.Series, pd.Series]] = {}
    for challenger in build_reentry_candidates():
        target, scores = positions_with_reentry(factors, baseline.rule, challenger)
        results = run_period_backtests(
            data.daily,
            target,
            VALIDATION_PERIODS,
            fee_rate=fee_rate,
            init_cash=init_cash,
        )
        metrics = _metrics(results)
        return_deltas = {
            name: float(metrics[name]["strategy_return"])
            - float(champion_metrics[name]["strategy_return"])
            for name in VALIDATION_PERIODS
        }
        sharpe_deltas = {
            name: float(metrics[name]["sharpe"])
            - float(champion_metrics[name]["sharpe"])
            for name in VALIDATION_PERIODS
        }
        passes = {
            name: window_passes(champion_metrics[name], metrics[name])
            for name in VALIDATION_PERIODS
        }
        row: dict[str, object] = {
            "candidate_id": challenger.candidate_id,
            "cooldown_days": challenger.cooldown_days,
            "reentry_gate": challenger.reentry_gate,
            "pass_count": sum(passes.values()),
            "min_return_delta": min(return_deltas.values()),
            "min_sharpe_delta": min(sharpe_deltas.values()),
            "mean_return_delta": float(np.mean(list(return_deltas.values()))),
            "mean_sharpe_delta": float(np.mean(list(sharpe_deltas.values()))),
            "complexity": challenger.complexity,
        }
        for name in VALIDATION_PERIODS:
            row[f"{name}_return"] = metrics[name]["strategy_return"]
            row[f"{name}_sharpe"] = metrics[name]["sharpe"]
            row[f"{name}_return_delta"] = return_deltas[name]
            row[f"{name}_sharpe_delta"] = sharpe_deltas[name]
            row[f"{name}_pass"] = passes[name]
        rows.append(row)
        candidate_payloads[challenger.candidate_id] = (challenger, target, scores)

    ranked = rank_challengers(pd.DataFrame(rows))
    best_id = str(ranked.iloc[0]["candidate_id"])
    best, best_target, best_scores = candidate_payloads[best_id]
    overall_pass = int(ranked.iloc[0]["pass_count"]) == len(VALIDATION_PERIODS)
    frozen = None
    audit = None
    if overall_pass:
        factor_output = factor_result.frame.copy()
        factor_output.insert(0, "factor_score", best_scores)
        factor_output.insert(0, "target_position", best_target)
        factor_output.insert(2, "enter_threshold", baseline.rule.enter)
        factor_output.insert(3, "exit_threshold", baseline.rule.exit)
        events = build_factor_events(best_target, best_scores, factor_output, baseline.rule)
        formal_results = run_period_backtests(
            data.daily,
            best_target,
            VALIDATION_PERIODS,
            fee_rate=fee_rate,
            init_cash=init_cash,
            factor_events=events,
            factor_frame=factor_output,
        )
        orders = pd.concat([result.orders for result in formal_results.values()], ignore_index=True)
        used = pd.concat(
            [events, *[result.factor_events for result in formal_results.values()]],
            ignore_index=True,
        ).drop_duplicates(subset=["event_id"])
        audit = audit_no_lookahead(orders, used, best_target, factor_output)
        frozen = {
            "schema_version": 1,
            "experiment": "588080_reentry_v1",
            "sample_end": str(RESEARCH_CUTOFF.date()),
            "champion": {"version": baseline.version, "sha256": baseline.sha256},
            "challenger": asdict(best),
            "candidate_id": best.candidate_id,
            "pass_rule": "strictly higher return and Sharpe in 2023, 2024 and 2025",
            "research_audit": audit,
        }
        _write_json(output_dir / "frozen_challenger.json", frozen)

    ranked.to_csv(output_dir / "candidate_results.csv", index=False, encoding="utf-8-sig")
    _write_json(output_dir / "champion_metrics.json", champion_metrics)
    _write_json(
        output_dir / "metrics.json",
        {
            "status": "PASS" if overall_pass else "FAIL",
            "best_candidate": best_id,
            "best_pass_count": int(ranked.iloc[0]["pass_count"]),
            "validation_periods": {
                name: {"start": str(start.date()), "end": str(end.date())}
                for name, (start, end) in VALIDATION_PERIODS.items()
            },
            "sample_cutoff": str(RESEARCH_CUTOFF.date()),
            "visible_data_hashes": data.hashes,
            "audit": audit,
        },
    )
    return {
        "status": "PASS" if overall_pass else "FAIL",
        "best_candidate": best_id,
        "best_pass_count": int(ranked.iloc[0]["pass_count"]),
        "frozen_challenger": frozen,
        "output_dir": str(output_dir.resolve()),
    }


def run_2026_holdout(
    raw_dir: Path,
    baseline_root: Path,
    frozen_challenger_path: Path,
    output_dir: Path,
    *,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Evaluate one already-frozen challenger; never search or change parameters."""
    frozen = json.loads(Path(frozen_challenger_path).read_text(encoding="utf-8"))
    if frozen.get("sample_end") != "2025-12-31":
        raise ValueError("challenger must be frozen from the pre-2026 sample")
    challenger_payload = frozen.get("challenger")
    if not isinstance(challenger_payload, dict):
        raise ValueError("frozen challenger is missing its rule")
    challenger = ReentryChallenger(
        cooldown_days=int(challenger_payload["cooldown_days"]),
        reentry_gate=str(challenger_payload["reentry_gate"]),
    )
    baseline_version = str(frozen.get("champion", {}).get("version", ""))
    baseline = resolve_baseline(Path(baseline_root), baseline_version)
    if baseline.sha256 != str(frozen.get("champion", {}).get("sha256", "")):
        raise ValueError("frozen challenger champion hash differs from registry")

    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"holdout output is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    cutoff = max(end for _, end in TARGET_PERIODS.values())
    data = load_market_data(Path(raw_dir), "588080.SH", "etf", cutoff=cutoff)
    factor_result = generate_factor_frame(data)
    factors = factor_result.frame.loc[:, FACTOR_COLUMNS]
    champion_target, _ = positions_with_reentry(
        factors, baseline.rule, ReentryChallenger(0, "none")
    )
    challenger_target, challenger_scores = positions_with_reentry(
        factors, baseline.rule, challenger
    )
    champion_results = run_period_backtests(
        data.daily,
        champion_target,
        TARGET_PERIODS,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    factor_output = factor_result.frame.copy()
    factor_output.insert(0, "factor_score", challenger_scores)
    factor_output.insert(0, "target_position", challenger_target)
    factor_output.insert(2, "enter_threshold", baseline.rule.enter)
    factor_output.insert(3, "exit_threshold", baseline.rule.exit)
    events = build_factor_events(
        challenger_target, challenger_scores, factor_output, baseline.rule
    )
    challenger_results = run_period_backtests(
        data.daily,
        challenger_target,
        TARGET_PERIODS,
        fee_rate=fee_rate,
        init_cash=init_cash,
        factor_events=events,
        factor_frame=factor_output,
    )
    champion_metrics = _metrics(champion_results)
    challenger_metrics = _metrics(challenger_results)
    passes = {
        name: window_passes(champion_metrics[name], challenger_metrics[name])
        for name in TARGET_PERIODS
    }
    overall_pass = all(passes.values())
    orders = pd.concat(
        [result.orders for result in challenger_results.values()], ignore_index=True
    )
    used = pd.concat(
        [events, *[result.factor_events for result in challenger_results.values()]],
        ignore_index=True,
    ).drop_duplicates(subset=["event_id"])
    audit = audit_no_lookahead(orders, used, challenger_target, factor_output)
    windows = {
        name: {
            "champion_return": champion_metrics[name]["strategy_return"],
            "challenger_return": challenger_metrics[name]["strategy_return"],
            "return_delta": float(challenger_metrics[name]["strategy_return"])
            - float(champion_metrics[name]["strategy_return"]),
            "champion_sharpe": champion_metrics[name]["sharpe"],
            "challenger_sharpe": challenger_metrics[name]["sharpe"],
            "sharpe_delta": float(challenger_metrics[name]["sharpe"])
            - float(champion_metrics[name]["sharpe"]),
            "pass": passes[name],
        }
        for name in TARGET_PERIODS
    }
    payload = {
        "status": "PASS" if overall_pass else "FAIL",
        "champion": frozen["champion"],
        "challenger": challenger_payload,
        "windows": windows,
        "audit": audit,
        "holdout_cutoff": str(cutoff.date()),
        "data_hashes": data.hashes,
    }
    _write_json(output_dir / "metrics.json", payload)
    orders.to_csv(output_dir / "orders.csv", index=False, encoding="utf-8-sig")
    return {**payload, "output_dir": str(output_dir.resolve())}
