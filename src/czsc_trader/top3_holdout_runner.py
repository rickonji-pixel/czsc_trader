"""Freeze EX08 Top-3 selections, then evaluate them together on 2026."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import optuna
import pandas as pd

from .audit import audit_no_lookahead
from .backtest import run_period_backtests
from .data import load_market_data
from .experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from .factor_discovery import generate_candidate_factors, validate_sparse_weights
from .factor_discovery_runner import _metric_view, _target
from .factors import generate_factor_frame
from .four_layer import normalized_signal_factors
from .four_layer_runner import _events, _metrics, _target_digest, _write_json
from .objectives import TARGET_PERIODS
from .optuna_runner import prepare_selection_inputs, validate_ex08_protocol
from .optuna_search import suggest_trial_parameters


EXPECTED_RULES = {
    "robustness_first": {
        "sort": [
            "min_return_delta",
            "win_count",
            "median_return_delta",
            "mean_return_delta",
        ],
        "top_trials": [3123, 2806, 629],
    },
    "win_count_first": {
        "sort": [
            "win_count",
            "mean_return_delta",
            "median_return_delta",
            "min_return_delta",
        ],
        "top_trials": [2385, 2806, 2478],
    },
    "mean_return_first": {
        "sort": [
            "mean_return_delta",
            "median_return_delta",
            "min_return_delta",
        ],
        "top_trials": [3384, 1192, 2214],
    },
}
EXPECTED_UNIQUE_TRIALS = (629, 1192, 2214, 2385, 2478, 2806, 3123, 3384)


def validate_tournament_protocol(protocol: Mapping[str, object]) -> None:
    """Require the complete preregistered 0825_EX01 contract."""
    expected = {
        "schema_version": 1,
        "experiment_id": "0825_EX01",
        "experiment_type": "ex08_top3_holdout_tournament",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "selection_sample_end": "2025-12-31",
        "source": {
            "experiment": "0824_EX08",
            "execution_commit": "e5341a2a684fdd779c6d58a55094e8abd83a2ede",
            "archive_commit": "31212ff10e3183000e301dd93b417f209037861f",
            "trials_manifest_sha256": "13e852f38960bf86cec5e0b8124ae147584a7e6ab02ab7fc1922d310c6a426b4",
            "ranking_manifest_sha256": "159bfbafa9125c3837eba6d11f769a849afb279fd1ab16acc3193fa0ad61c775",
        },
        "research_baseline": {
            "experiment": "0824_EX04",
            "file": "experiments/0824_EX04/artifacts/frozen_challenger.json",
            "sha256": "c6fa86c0f87743564231dec4fb6e66971bde0fa4107829d077cb5cf31c53885f",
        },
        "exclude_control_trials": [0],
        "rules": EXPECTED_RULES,
        "frozen_unique_trials": list(EXPECTED_UNIQUE_TRIALS),
        "holdout_windows": ["2026Q1", "2026H1", "2026M1-M8"],
        "candidate_pass_rule": "strictly higher return than EX04 in every holdout window and causal audit PASS",
        "experiment_pass_rule": "at least one frozen candidate PASS",
        "sharpe_in_pass": False,
        "holdout_access_before_freeze": False,
        "search_or_parameter_updates": False,
        "holdout_load_count": 1,
    }
    if dict(protocol) != expected:
        raise ValueError("0825_EX01 protocol differs from preregistration")


def select_rule_top3(
    ranking: pd.DataFrame, protocol: Mapping[str, object]
) -> dict[str, tuple[int, ...]]:
    """Re-rank committed EX08 rows under each frozen rule."""
    validate_tournament_protocol(protocol)
    if ranking["trial_number"].duplicated().any():
        raise ValueError("EX08 ranking contains duplicate trial numbers")
    eligible = ranking.loc[
        ~ranking["trial_number"].isin(protocol["exclude_control_trials"])
    ].copy()
    selected: dict[str, tuple[int, ...]] = {}
    for name, rule in EXPECTED_RULES.items():
        ordered = eligible.sort_values(
            list(rule["sort"]), ascending=False, kind="mergesort"
        )
        actual = tuple(int(value) for value in ordered.head(3)["trial_number"])
        expected = tuple(int(value) for value in rule["top_trials"])
        if actual != expected:
            raise ValueError(f"{name} Top3 differs from preregistration")
        selected[name] = actual
    unique = tuple(sorted({trial for values in selected.values() for trial in values}))
    if unique != EXPECTED_UNIQUE_TRIALS:
        raise ValueError("unique frozen trials differ from preregistration")
    return selected


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _trial_params(row: pd.Series) -> dict[str, object]:
    params: dict[str, object] = {}
    for column, value in row.items():
        if not str(column).startswith("param::") or pd.isna(value):
            continue
        name = str(column).removeprefix("param::")
        params[name] = int(value) if name == "active_factor_count" else float(value)
    return params


def freeze_tournament_candidates(
    repo_root: Path, experiment_dir: Path
) -> tuple[dict[str, object], str]:
    """Freeze the preregistered EX08 selections without opening 2026 data."""
    repo_root = Path(repo_root).resolve()
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    validate_tournament_protocol(protocol)

    source_dir = repo_root / "experiments" / str(protocol["source"]["experiment"])
    source_manifest = validate_experiment_archive(source_dir)
    files = source_manifest["files"]
    expected_records = {
        "artifacts/trials.csv": protocol["source"]["trials_manifest_sha256"],
        "artifacts/trial_ranking.csv": protocol["source"]["ranking_manifest_sha256"],
    }
    for name, expected in expected_records.items():
        if files.get(name, {}).get("sha256") != expected:
            raise ValueError(f"EX08 source manifest hash differs: {name}")

    ranking = pd.read_csv(
        source_dir / "artifacts" / "trial_ranking.csv", encoding="utf-8-sig"
    )
    selections = select_rule_top3(ranking, protocol)
    evidence_rows: list[dict[str, object]] = []
    for rule_name, trial_numbers in selections.items():
        for rank, trial_number in enumerate(trial_numbers, start=1):
            row = ranking.loc[ranking["trial_number"].eq(trial_number)].iloc[0]
            evidence_rows.append(
                {
                    "rule": rule_name,
                    "rank": rank,
                    "trial_number": trial_number,
                    "win_count": int(row["win_count"]),
                    "min_return_delta": float(row["min_return_delta"]),
                    "median_return_delta": float(row["median_return_delta"]),
                    "mean_return_delta": float(row["mean_return_delta"]),
                }
            )
    pd.DataFrame(evidence_rows).to_csv(
        artifacts / "selection_rule_top3.csv", index=False, encoding="utf-8-sig"
    )

    context = prepare_selection_inputs(
        repo_root / "data" / "raw",
        repo_root / "configs" / "rule_baselines",
        source_dir,
        protocol_validator=validate_ex08_protocol,
    )
    trials = pd.read_csv(source_dir / "artifacts" / "trials.csv", encoding="utf-8-sig")
    candidates: list[dict[str, object]] = []
    for trial_number in EXPECTED_UNIQUE_TRIALS:
        matches = trials.loc[trials["number"].eq(trial_number)]
        if len(matches) != 1 or matches.iloc[0]["state"] != "COMPLETE":
            raise ValueError(f"EX08 Trial {trial_number} is not uniquely COMPLETE")
        trial_row = matches.iloc[0]
        strategy = suggest_trial_parameters(
            optuna.trial.FixedTrial(_trial_params(trial_row)),
            tuple(context.candidate.factors.columns),
            context.origin_weights,
            context.protocol,
        )
        target, _ = _target(
            context.candidate.factors,
            strategy.weights,
            strategy.enter,
            strategy.exit,
            context.baseline.rule,
        )
        digest = _target_digest(target)
        if digest != str(trial_row["attr::target_digest"]):
            raise ValueError(f"EX08 Trial {trial_number} target digest differs")
        active = strategy.weights[strategy.weights.ne(0.0)]
        ranking_row = ranking.loc[ranking["trial_number"].eq(trial_number)].iloc[0]
        memberships = [
            name for name, values in selections.items() if trial_number in values
        ]
        candidates.append(
            {
                "trial_number": trial_number,
                "selected_by": memberships,
                "selection_metrics": {
                    "win_count": int(ranking_row["win_count"]),
                    "min_return_delta": float(ranking_row["min_return_delta"]),
                    "median_return_delta": float(ranking_row["median_return_delta"]),
                    "mean_return_delta": float(ranking_row["mean_return_delta"]),
                },
                "factor_names": list(active.index),
                "weights": active.to_dict(),
                "enter": float(strategy.enter),
                "exit": float(strategy.exit),
                "selection_target_digest": digest,
            }
        )
    frozen = {
        "schema_version": 1,
        "experiment": str(protocol["experiment_id"]),
        "sample_end": str(protocol["selection_sample_end"]),
        "source": protocol["source"],
        "research_baseline": protocol["research_baseline"],
        "rules": protocol["rules"],
        "candidates": candidates,
        "holdout_accessed": False,
    }
    frozen_path = artifacts / "frozen_candidates.json"
    _write_json(frozen_path, frozen)
    return frozen, sha256(frozen_path.read_bytes()).hexdigest()


def candidate_window(
    ex04: Mapping[str, object], candidate: Mapping[str, object]
) -> dict[str, object]:
    """Compare one frozen candidate with EX04 in one holdout window."""
    ex04_return = float(ex04["strategy_return"])
    candidate_return = float(candidate["strategy_return"])
    return {
        "ex04": _metric_view(ex04),
        "candidate": _metric_view(candidate),
        "ex04_return": ex04_return,
        "candidate_return": candidate_return,
        "return_delta_vs_ex04": candidate_return - ex04_return,
        "ex04_sharpe": float(ex04["sharpe"]),
        "candidate_sharpe": float(candidate["sharpe"]),
        "sharpe_delta_vs_ex04": float(candidate["sharpe"])
        - float(ex04["sharpe"]),
        "pass": candidate_return > ex04_return,
    }


def run_tournament_holdout(
    repo_root: Path,
    experiment_dir: Path,
    frozen_digest: str,
) -> dict[str, object]:
    """Load 2026 once and evaluate every preregistered frozen candidate."""
    repo_root = Path(repo_root).resolve()
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    frozen_path = artifacts / "frozen_candidates.json"
    actual_digest = sha256(frozen_path.read_bytes()).hexdigest()
    if actual_digest != frozen_digest:
        raise ValueError("frozen candidate hash changed before holdout")
    frozen = _read_json(frozen_path)
    if frozen.get("holdout_accessed") is not False:
        raise ValueError("frozen candidates do not preserve the holdout boundary")
    candidates = frozen.get("candidates")
    if not isinstance(candidates, list) or tuple(
        int(candidate["trial_number"]) for candidate in candidates
    ) != EXPECTED_UNIQUE_TRIALS:
        raise ValueError("frozen candidate identity differs before holdout")

    source_dir = repo_root / "experiments" / "0824_EX08"
    context = prepare_selection_inputs(
        repo_root / "data" / "raw",
        repo_root / "configs" / "rule_baselines",
        source_dir,
        protocol_validator=validate_ex08_protocol,
    )
    cutoff = max(end for _, end in TARGET_PERIODS.values())
    data = load_market_data(
        repo_root / "data" / "raw", "588080.SH", "etf", cutoff=cutoff
    )
    ex04_weights = pd.Series(context.ex04["weights"], dtype=float)
    frozen_names: list[str] = []
    for candidate in candidates:
        frozen_names.extend(map(str, candidate["factor_names"]))
    factor_matrix = generate_candidate_factors(
        data,
        context.ex06_protocol,
        ex04_weights,
        frozen_names=list(dict.fromkeys(frozen_names)),
    )

    legacy_frame = generate_factor_frame(data).frame
    existing = normalized_signal_factors(legacy_frame.filter(like="raw__"))
    ex04_target, _ = _target(
        existing,
        ex04_weights,
        float(context.ex04["spec"]["enter"]),
        float(context.ex04["spec"]["exit"]),
        context.baseline.rule,
    )
    ex04_metrics = _metrics(run_period_backtests(data.daily, ex04_target, TARGET_PERIODS))

    candidate_results: dict[str, object] = {}
    audits: dict[str, object] = {}
    order_frames: list[pd.DataFrame] = []
    ranking_rows: list[dict[str, object]] = []
    for frozen_candidate in candidates:
        trial_number = int(frozen_candidate["trial_number"])
        weights = pd.Series(frozen_candidate["weights"], dtype=float)
        validation_protocol = dict(context.ex06_protocol)
        validation_protocol["maximum_active_factors"] = 18
        validate_sparse_weights(weights, weights.index, validation_protocol)
        target, scores = _target(
            factor_matrix.factors.loc[:, list(weights.index)],
            weights,
            float(frozen_candidate["enter"]),
            float(frozen_candidate["exit"]),
            context.baseline.rule,
        )
        events = _events(
            target,
            scores,
            float(frozen_candidate["enter"]),
            float(frozen_candidate["exit"]),
        )
        audit_frame = pd.DataFrame(
            {
                "factor_score": scores,
                "enter_threshold": float(frozen_candidate["enter"]),
                "exit_threshold": float(frozen_candidate["exit"]),
            },
            index=scores.index,
        )
        backtests = run_period_backtests(
            data.daily,
            target,
            TARGET_PERIODS,
            factor_events=events,
            factor_frame=audit_frame,
        )
        metrics = _metrics(backtests)
        windows = {
            name: candidate_window(ex04_metrics[name], metrics[name])
            for name in TARGET_PERIODS
        }
        orders = pd.concat(
            [result.orders for result in backtests.values()], ignore_index=True
        )
        used_events = pd.concat(
            [events, *[result.factor_events for result in backtests.values()]],
            ignore_index=True,
        ).drop_duplicates(subset=["event_id"])
        audit = audit_no_lookahead(orders, used_events, target, audit_frame)
        candidate_pass = all(bool(row["pass"]) for row in windows.values()) and (
            audit["status"] == "PASS"
        )
        if not orders.empty:
            orders.insert(0, "trial_number", trial_number)
            order_frames.append(orders)
        key = str(trial_number)
        audits[key] = audit
        candidate_results[key] = {
            "trial_number": trial_number,
            "selected_by": frozen_candidate["selected_by"],
            "selection_metrics": frozen_candidate["selection_metrics"],
            "status": "PASS" if candidate_pass else "FAIL",
            "windows": windows,
            "audit": audit,
        }
        deltas = [float(row["return_delta_vs_ex04"]) for row in windows.values()]
        ranking_rows.append(
            {
                "trial_number": trial_number,
                "status": "PASS" if candidate_pass else "FAIL",
                "selected_by": ";".join(frozen_candidate["selected_by"]),
                "holdout_win_count": sum(delta > 0.0 for delta in deltas),
                "mean_return_delta": sum(deltas) / len(deltas),
                "min_return_delta": min(deltas),
            }
        )

    holdout_ranking = pd.DataFrame(ranking_rows).sort_values(
        ["holdout_win_count", "mean_return_delta", "min_return_delta"],
        ascending=False,
        kind="mergesort",
    )
    holdout_ranking.to_csv(
        artifacts / "holdout_ranking.csv", index=False, encoding="utf-8-sig"
    )
    if order_frames:
        pd.concat(order_frames, ignore_index=True).to_csv(
            artifacts / "orders.csv", index=False, encoding="utf-8-sig"
        )
    else:
        pd.DataFrame(columns=["trial_number"]).to_csv(
            artifacts / "orders.csv", index=False, encoding="utf-8-sig"
        )
    _write_json(artifacts / "causal_audits.json", audits)
    overall = any(row["status"] == "PASS" for row in candidate_results.values())
    payload = {
        "status": "PASS" if overall else "FAIL",
        "frozen_candidates_sha256": frozen_digest,
        "frozen_before_holdout": True,
        "holdout_cutoff": str(cutoff.date()),
        "holdout_load_count": 1,
        "holdout_data_hashes": data.hashes,
        "holdout_accessed": True,
        "ex04": ex04_metrics,
        "candidates": candidate_results,
    }
    _write_json(artifacts / "holdout_metrics.json", payload)
    return payload


def _write_documents(
    experiment_dir: Path,
    holdout: Mapping[str, object],
    frozen_digest: str,
    execution_commit: str,
) -> None:
    candidates = holdout["candidates"]
    passed = [
        int(number)
        for number, result in candidates.items()
        if result["status"] == "PASS"
    ]
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 状态：正式执行完成（`{holdout['status']}`）\n"
        f"- 执行提交：`{execution_commit}`\n"
        "- Optuna重跑：否\n"
        "- EX08重排规则：3套；每套Top 3\n"
        "- 冻结唯一候选：8项\n"
        f"- 冻结SHA-256：`{frozen_digest}`\n"
        "- 冻结前访问2026：否\n"
        "- 2026行情加载：1次\n"
        "- 候选因果审计：全部见`artifacts/causal_audits.json`\n",
        encoding="utf-8",
    )
    lines = [
        "# 研究结论",
        "",
        f"EX08三规则Top 3补测结果为 **{holdout['status']}**。",
        "",
        f"- PASS候选：{', '.join(map(str, passed)) if passed else '无'}。",
        "- 逐候选逐窗口指标见`artifacts/holdout_metrics.json`。",
        "- 2026报告排名见`artifacts/holdout_ranking.csv`。",
        "- 本轮未重跑Optuna，未根据2026修改规则或参数。",
    ]
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def run_tournament_experiment(
    repo_root: Path, experiment_dir: Path, *, execution_commit: str
) -> dict[str, object]:
    """Freeze EX08 Top3 candidates, unlock 2026 once, and archive results."""
    repo_root = Path(repo_root).resolve()
    experiment_dir = Path(experiment_dir).resolve()
    started = datetime.now(timezone.utc).isoformat()
    frozen, frozen_digest = freeze_tournament_candidates(repo_root, experiment_dir)
    holdout = run_tournament_holdout(repo_root, experiment_dir, frozen_digest)
    _write_documents(experiment_dir, holdout, frozen_digest, execution_commit)
    summary = {
        "status": holdout["status"],
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_commit": execution_commit,
        "source_experiment": "0824_EX08",
        "rule_count": 3,
        "frozen_unique_trial_count": len(frozen["candidates"]),
        "frozen_candidates_sha256": frozen_digest,
        "holdout_accessed": True,
        "holdout_load_count": 1,
    }
    _write_json(experiment_dir / "artifacts" / "study_summary.json", summary)
    protocol = _read_json(experiment_dir / "artifacts" / "protocol.json")
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": "0825_EX01",
            "date": "2026-08-25",
            "status": holdout["status"],
            "symbol": "588080.SH",
            "asset_type": "etf",
            "research_baseline": protocol["research_baseline"],
            "source_experiment": "0824_EX08",
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": True,
            "frozen_candidates_sha256": frozen_digest,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {
        "status": holdout["status"],
        "passed_trials": [
            int(number)
            for number, result in holdout["candidates"].items()
            if result["status"] == "PASS"
        ],
        "experiment_dir": str(experiment_dir),
    }


