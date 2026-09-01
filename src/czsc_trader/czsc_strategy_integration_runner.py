"""Run the preregistered unified CZSC factor integration experiment."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from .backtest import run_period_backtests
from .baselines import resolve_baseline
from .baseline_execution import apply_resolved_baseline
from .czsc_factor_stability import event_onsets
from .czsc_route_runner import _build_route_family, _raw_signals
from .czsc_strategy_integration import integrate_event_factor, strategy_candidate_passes
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import generate_factor_frame
from .four_layer import normalized_signal_factors, positions_from_scores


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _validate(protocol: Mapping[str, object]) -> None:
    expected = {
        "experiment_id": "0901_EX13",
        "handler": "czsc_unified_factor_integration",
        "symbol": "588080.SH",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "source_experiment": "0901_EX08",
        "source_factor_kind": "event",
        "factor_direction": 1,
        "champion_relative_weights_frozen": True,
        "thresholds_frozen": True,
        "state_machine_frozen": True,
        "strategy_round": 1,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"unified-integration protocol {key} differs from preregistration")
    weights = list(map(float, protocol.get("event_weights", [])))
    if weights != [0.025, 0.05, 0.075, 0.1, 0.15, 0.2]:
        raise ValueError("unified-integration event weights differ from preregistration")


def _strategy_inputs(data: object, baseline: object, source_protocol: Mapping[str, object], source_dir: Path, source_factor: str) -> tuple[pd.DataFrame, pd.Series, pd.Series, object]:
    frame = generate_factor_frame(data).frame
    champion = apply_resolved_baseline(frame, baseline)
    names = list(map(str, baseline.factor_names))
    factors = normalized_signal_factors(frame[names]).astype(float)
    weights = pd.Series(baseline.factor_weights, index=names, dtype=float)
    raw = _raw_signals(data, source_protocol)
    source_factors, _ = _build_route_family(raw, str(source_protocol["family"]), source_protocol, source_dir)
    if source_factor not in source_factors:
        raise ValueError("unified-integration source factor cannot be replayed")
    event = event_onsets(source_factors[source_factor]).astype(float).reindex(factors.index)
    if event.isna().any():
        raise ValueError("unified-integration event is not aligned to champion factors")
    return factors, weights, event, champion


def _target(factors: pd.DataFrame, weights: pd.Series, event: pd.Series, baseline: object, event_weight: float) -> tuple[pd.Series, pd.Series]:
    scores = integrate_event_factor(factors, weights, event, event_weight=event_weight)
    target = positions_from_scores(scores, baseline.rule.enter, baseline.rule.exit, baseline.rule)
    return scores, target


def _periods(protocol: Mapping[str, object]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    periods = {
        "2021_2025": (
            pd.Timestamp(str(protocol["evaluation_start"])),
            pd.Timestamp(str(protocol["evaluation_end"])),
        )
    }
    for year in range(2021, 2026):
        periods[str(year)] = (pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-12-31"))
    return periods


def run_unified_factor_integration(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    _validate(protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH")
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("unified-integration champion identity differs")
    parent_path = experiment_dir.parent / str(protocol["interaction_parent"]) / "experiment_manifest.json"
    parent_hash = sha256(parent_path.read_bytes()).hexdigest()
    if parent_hash != str(protocol["interaction_parent_manifest"]):
        raise ValueError("unified-integration parent identity differs")

    condition_dir = experiment_dir.parent / str(protocol["champion_condition_parent"])
    confirmed = pd.read_csv(condition_dir / "artifacts" / "confirmed_factors.csv")
    if set(map(int, confirmed["champion_target_position"])) != {0, 1}:
        raise ValueError("unified-integration source was not confirmed in both champion positions")
    source_dir = experiment_dir.parent / str(protocol["source_experiment"])
    source_protocol = json.loads((source_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8"))
    source_factor = str(protocol["source_factor"])
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["visible_end"])))
    factors, champion_weights, event, champion = _strategy_inputs(data, baseline, source_protocol, source_dir, source_factor)
    periods = _periods(protocol)
    fee_rate = float(protocol["fee_rate"])
    init_cash = float(protocol["init_cash"])
    champion_results = run_period_backtests(data.daily, champion.target_position, periods, fee_rate=fee_rate, init_cash=init_cash)
    champion_metrics = {name: result.metrics for name, result in champion_results.items()}

    targets: dict[float, pd.Series] = {}
    scores_by_weight: dict[float, pd.Series] = {}
    result_sets: dict[float, dict[str, object]] = {}
    rows: list[dict[str, object]] = []
    main_champion = champion_metrics["2021_2025"]
    for event_weight in map(float, protocol["event_weights"]):
        scores, target = _target(factors, champion_weights, event, baseline, event_weight)
        results = run_period_backtests(data.daily, target, periods, fee_rate=fee_rate, init_cash=init_cash)
        metrics = {name: result.metrics for name, result in results.items()}
        main = metrics["2021_2025"]
        passed = strategy_candidate_passes(
            main_champion,
            main,
            return_tolerance=float(protocol["return_tolerance"]),
            drawdown_tolerance=float(protocol["max_drawdown_tolerance"]),
        )
        row: dict[str, object] = {
            "event_weight": event_weight,
            "pass": passed,
            "return_floor_pass": float(main["strategy_return"]) >= float(main_champion["strategy_return"]) - float(protocol["return_tolerance"]),
            "strategy_return": float(main["strategy_return"]),
            "return_delta": float(main["strategy_return"]) - float(main_champion["strategy_return"]),
            "max_drawdown": float(main["max_drawdown"]),
            "max_drawdown_delta": float(main["max_drawdown"]) - float(main_champion["max_drawdown"]),
            "sharpe": float(main["sharpe"]),
            "sharpe_delta": float(main["sharpe"]) - float(main_champion["sharpe"]),
            "trade_count": int(main["trade_count"]),
            "exposure": float(main["exposure"]),
        }
        for year in range(2021, 2026):
            ym = metrics[str(year)]
            row[f"{year}_return"] = float(ym["strategy_return"])
            row[f"{year}_max_drawdown"] = float(ym["max_drawdown"])
            row[f"{year}_sharpe"] = float(ym["sharpe"])
        rows.append(row)
        targets[event_weight] = target
        scores_by_weight[event_weight] = scores
        result_sets[event_weight] = results
    candidates = pd.DataFrame(rows)
    candidates = candidates.sort_values(
        ["pass", "return_floor_pass", "max_drawdown", "strategy_return", "sharpe", "event_weight"],
        ascending=[False, False, False, False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    candidates.insert(0, "rank", range(1, len(candidates) + 1))
    candidates.to_csv(artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig")
    selected_weight = float(candidates.iloc[0]["event_weight"])
    selected_target = targets[selected_weight]
    selected_scores = scores_by_weight[selected_weight]
    selected_results = result_sets[selected_weight]
    status = "PASS" if bool(candidates.iloc[0]["pass"]) else "FAIL"

    prefix_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp("2023-12-31"))
    prefix_factors, prefix_weights, prefix_event, _ = _strategy_inputs(prefix_data, baseline, source_protocol, source_dir, source_factor)
    _, prefix_target = _target(prefix_factors, prefix_weights, prefix_event, baseline, selected_weight)
    prefix_equal = prefix_target.equals(selected_target.reindex(prefix_target.index))
    orders = selected_results["2021_2025"].orders.copy()
    next_open = bool(orders.empty or (pd.to_datetime(orders["signal_date"]) < pd.to_datetime(orders["execution_date"])).all())
    causal_audit = {
        "status": "PASS" if prefix_equal and next_open else "FAIL",
        "prefix_cutoff": "2023-12-31",
        "prefix_target_equal": prefix_equal,
        "all_orders_signal_before_execution": next_open,
        "execution_rule": "signal date close to next trading session open",
        "access_2026": False,
    }
    _write_json(artifacts / "causal_strategy_audit.json", causal_audit)
    orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        {
            "signal_date": selected_target.index,
            "source_event": event.to_numpy(),
            "champion_score": champion.scores.reindex(selected_target.index).to_numpy(),
            "challenger_score": selected_scores.to_numpy(),
            "champion_target": champion.target_position.reindex(selected_target.index).to_numpy(),
            "challenger_target": selected_target.to_numpy(),
        }
    ).to_csv(artifacts / "position_path.csv", index=False, encoding="utf-8-sig")

    sensitivity_rows: list[dict[str, object]] = []
    main_period = {"2021_2025": periods["2021_2025"]}
    for fee in map(float, protocol["fee_sensitivity"]):
        cm = run_period_backtests(data.daily, champion.target_position, main_period, fee_rate=fee, init_cash=init_cash)["2021_2025"].metrics
        sm = run_period_backtests(data.daily, selected_target, main_period, fee_rate=fee, init_cash=init_cash)["2021_2025"].metrics
        sensitivity_rows.append(
            {
                "fee_rate": fee,
                "champion_return": float(cm["strategy_return"]),
                "challenger_return": float(sm["strategy_return"]),
                "return_delta": float(sm["strategy_return"]) - float(cm["strategy_return"]),
                "champion_max_drawdown": float(cm["max_drawdown"]),
                "challenger_max_drawdown": float(sm["max_drawdown"]),
                "max_drawdown_delta": float(sm["max_drawdown"]) - float(cm["max_drawdown"]),
            }
        )
    pd.DataFrame(sensitivity_rows).to_csv(artifacts / "fee_sensitivity.csv", index=False, encoding="utf-8-sig")
    comparison_rows = []
    for name in periods:
        cm = champion_metrics[name]
        sm = selected_results[name].metrics
        comparison_rows.append(
            {
                "period": name,
                "champion_return": float(cm["strategy_return"]),
                "challenger_return": float(sm["strategy_return"]),
                "return_delta": float(sm["strategy_return"]) - float(cm["strategy_return"]),
                "champion_max_drawdown": float(cm["max_drawdown"]),
                "challenger_max_drawdown": float(sm["max_drawdown"]),
                "max_drawdown_delta": float(sm["max_drawdown"]) - float(cm["max_drawdown"]),
                "champion_sharpe": float(cm["sharpe"]),
                "challenger_sharpe": float(sm["sharpe"]),
                "sharpe_delta": float(sm["sharpe"]) - float(cm["sharpe"]),
            }
        )
    pd.DataFrame(comparison_rows).to_csv(artifacts / "period_comparison.csv", index=False, encoding="utf-8-sig")
    if status == "PASS" and causal_audit["status"] == "PASS":
        effective_weights = {
            str(name): float(weight) * (1.0 - selected_weight)
            for name, weight in zip(
                baseline.factor_names, baseline.factor_weights, strict=True
            )
        }
        effective_weights[source_factor] = selected_weight
        _write_json(
            artifacts / "frozen_historical_challenger.json",
            {
                "schema_version": 1,
                "experiment": "0901_EX13",
                "sample_end": "2025-12-31",
                "champion": protocol["champion"],
                "source_factor": source_factor,
                "source_semantics": "positive one-day exit event",
                "factor_names": [*map(str, baseline.factor_names), source_factor],
                "weights": effective_weights,
                "event_weight": selected_weight,
                "champion_weight_scale": 1.0 - selected_weight,
                "entry_threshold": baseline.rule.enter,
                "exit_threshold": baseline.rule.exit,
                "confirm_days": baseline.rule.confirm_days,
                "min_hold_days": baseline.rule.min_hold_days,
                "exit_confirm_days": baseline.rule.exit_confirm_days,
                "execution": "next_session_open",
                "fee_rate": fee_rate,
            },
        )
    effective_status = status if causal_audit["status"] == "PASS" else "ERROR"
    summary = {
        "status": effective_status,
        "candidate_count": len(candidates),
        "passing_candidate_count": int(candidates["pass"].sum()),
        "selected_event_weight": selected_weight,
        "event_count": int(event.sum()),
        "champion": {key: main_champion[key] for key in ("strategy_return", "max_drawdown", "sharpe", "trade_count", "exposure")},
        "challenger": {key: selected_results["2021_2025"].metrics[key] for key in ("strategy_return", "max_drawdown", "sharpe", "trade_count", "exposure")},
        "causal_audit": causal_audit["status"],
        "visible_sample_end": "2025-12-31",
        "access_2026": False,
    }
    _write_json(artifacts / "metrics.json", summary)
    _write_json(
        artifacts / "identity_audit.json",
        {
            "status": "PASS",
            "champion_version": baseline.version,
            "champion_sha256": baseline.sha256,
            "interaction_parent_manifest": parent_hash,
            "source_factor": source_factor,
            "visible_data_hashes": data.hashes,
            "access_2026": False,
        },
    )
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0901_EX13 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`。",
                f"- 候选：{len(candidates)}个固定事件权重；通过硬门槛：{int(candidates['pass'].sum())}个。",
                f"- 诊断/入选权重：{selected_weight:.3f}；风险事件共{int(event.sum())}次。",
                f"- 因果前缀与下一开盘审计：`{causal_audit['status']}`。",
                "- 评价窗口为2021—2025；未读取2026。",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    main_selected = selected_results["2021_2025"].metrics
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0901_EX13 结论",
                "",
                f"状态：`{effective_status}`。",
                "",
                f"冠军净收益率/最大回撤：{float(main_champion['strategy_return']):.6f} / {float(main_champion['max_drawdown']):.6f}。",
                f"挑战者净收益率/最大回撤：{float(main_selected['strategy_return']):.6f} / {float(main_selected['max_drawdown']):.6f}。",
                f"收益差/最大回撤改善：{float(main_selected['strategy_return']) - float(main_champion['strategy_return']):.6f} / {float(main_selected['max_drawdown']) - float(main_champion['max_drawdown']):.6f}。",
                "PASS表示统一四层策略同时满足收益不下降和最大回撤严格改善；FAIL时只保留机制诊断，不冻结挑战者。",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": "0901_EX13",
            "date": "2026-09-01",
            "status": effective_status,
            "symbol": "588080.SH",
            "asset_type": "etf",
            "champion": protocol["champion"],
            "protocol_sha256": sha256((artifacts / "protocol.json").read_bytes()).hexdigest(),
            "visible_sample_end": "2025-12-31",
            "validation_accessed": False,
            "historical_2026_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
