"""Run the preregistered ER60 regime-conditioned weight challenge."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from itertools import product
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import PeriodBacktestResult, run_period_backtests
from .baseline_execution import apply_resolved_baseline
from .baselines import resolve_baseline
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import generate_factor_frame, signal_groups
from .four_layer import normalized_signal_factors, positions_from_scores
from .regime_weight import (
    candidate_relative_improvements,
    classify_regimes,
    closed_trade_ledger,
    fit_regime_threshold,
    lagged_efficiency_ratio,
    project_group_weights,
    risk_quality_metrics,
    score_with_regime_weights,
    strict_quality_pass,
)


EXPECTED_BASELINE_SHA = "fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822"
EXPECTED_MULTIPLIERS = [0.5, 0.75, 1.0, 1.25, 1.5]


def validate_regime_weight_protocol(protocol: Mapping[str, object]) -> None:
    """Reject any research boundary that differs from EX19 preregistration."""
    expected = {
        "schema_version": 1,
        "experiment_id": "0901_EX19",
        "handler": "regime_conditioned_weight_challenge",
        "experiment_type": "regime_conditioned_weight_challenge",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "calibration_start": "2020-01-01",
        "research_start": "2021-01-01",
        "research_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-08-28",
        "er_lookback": 60,
        "regime_labels": ["trend", "range", "warmup"],
        "er_threshold_method": "research_valid_median",
        "group_reference": "structure",
        "multipliers": EXPECTED_MULTIPLIERS,
        "candidate_count": 625,
        "entry_threshold": 0.175,
        "exit_threshold": 0.025,
        "fee_rate": 0.0005,
        "init_cash": 1_000_000.0,
        "research_min_closed_trades": 20,
        "test_min_closed_trades": 5,
        "annual_double_win_minimum": 3,
        "tolerance": 1e-12,
        "access_2026_after_freeze_only": True,
        "report_only_metrics": ["strategy_return", "sharpe", "exposure", "trade_count"],
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"regime-weight protocol {key} differs from preregistration")
    baseline = protocol.get("baseline")
    if not isinstance(baseline, Mapping):
        raise ValueError("regime-weight protocol baseline is missing")
    if baseline.get("version") != "baseline_20260826" or baseline.get("sha256") != EXPECTED_BASELINE_SHA:
        raise ValueError("regime-weight protocol baseline identity differs")


def candidate_grid(
    multipliers: Sequence[float],
) -> tuple[tuple[float, float, float, float], ...]:
    """Return trend-trend, trend-volume, range-trend, range-volume multipliers."""
    values = tuple(map(float, multipliers))
    if list(values) != EXPECTED_MULTIPLIERS:
        raise ValueError("regime-weight multiplier grid differs from preregistration")
    grid = tuple(product(values, repeat=4))
    if len(grid) != 625 or len(set(grid)) != 625:
        raise AssertionError("regime-weight grid identity is invalid")
    return grid


def research_candidate_passes(
    baseline: Mapping[str, object],
    challenger: Mapping[str, object],
    annual_double_wins: int,
    *,
    minimum_closed_trades: int = 20,
    annual_minimum: int = 3,
    tolerance: float = 1e-12,
) -> bool:
    """Apply continuous triple dominance plus annual drawdown-Calmar support."""
    return bool(
        int(annual_double_wins) >= int(annual_minimum)
        and strict_quality_pass(
            baseline,
            challenger,
            minimum_closed_trades=int(minimum_closed_trades),
            tolerance=float(tolerance),
        )
    )


def rank_research_candidates(rows: pd.DataFrame) -> pd.DataFrame:
    """Apply the deterministic preregistered candidate order."""
    required = {
        "candidate_id",
        "pass",
        "maximin_improvement",
        "annual_double_wins",
        "weight_shift",
    }
    if not required <= set(rows.columns):
        raise ValueError(f"candidate rows missing columns: {sorted(required - set(rows.columns))}")
    ranked = rows.sort_values(
        ["pass", "maximin_improvement", "annual_double_wins", "weight_shift", "candidate_id"],
        ascending=[False, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)
    ranked.insert(0, "rank", range(1, len(ranked) + 1))
    return ranked


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _daily_prices(data: object) -> pd.DataFrame:
    daily = data.daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"])
    return daily.set_index("dt").sort_index()


def _strategy_inputs(data: object, baseline: object) -> tuple[pd.DataFrame, pd.Series, object]:
    frame = generate_factor_frame(data).frame
    champion = apply_resolved_baseline(frame, baseline)
    names = list(map(str, baseline.factor_names))
    factors = normalized_signal_factors(frame[names]).astype(float)
    weights = pd.Series(baseline.factor_weights, index=names, name="weight", dtype=float)
    return factors, weights, champion


def _periods(start: str, end: str, *, include_years: bool) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    periods = {"continuous": (pd.Timestamp(start), pd.Timestamp(end))}
    if include_years:
        periods.update(
            {
                str(year): (pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-12-31"))
                for year in range(2021, 2026)
            }
        )
    return periods


def _metrics(result: PeriodBacktestResult, init_cash: float) -> dict[str, object]:
    quality = risk_quality_metrics(result.equity, result.orders, init_cash)
    return {
        **quality,
        "sharpe": float(result.metrics["sharpe"]),
        "exposure": float(result.metrics["exposure"]),
        "trade_count": int(result.metrics["trade_count"]),
        "start": str(result.metrics["start"]),
        "end": str(result.metrics["end"]),
    }


def _weights_for_candidate(
    base_weights: pd.Series,
    groups: Mapping[str, Sequence[str]],
    multipliers: tuple[float, float, float, float],
) -> dict[str, pd.Series]:
    trend_trend, trend_volume, range_trend, range_volume = multipliers
    return {
        "trend": project_group_weights(base_weights, groups, trend_trend, trend_volume),
        "range": project_group_weights(base_weights, groups, range_trend, range_volume),
    }


def _weight_shift(base_weights: pd.Series, weights: Mapping[str, pd.Series]) -> float:
    return float(
        sum((selected - base_weights).abs().sum() for selected in weights.values())
    )


def _annual_double_wins(
    baseline: Mapping[str, Mapping[str, object]],
    challenger: Mapping[str, Mapping[str, object]],
    tolerance: float,
) -> int:
    return sum(
        float(challenger[str(year)]["max_drawdown"])
        > float(baseline[str(year)]["max_drawdown"]) + float(tolerance)
        and float(challenger[str(year)]["calmar"])
        > float(baseline[str(year)]["calmar"]) + float(tolerance)
        for year in range(2021, 2026)
    )


def _next_open_audit(orders: pd.DataFrame) -> bool:
    if orders.empty:
        return True
    return bool(
        (pd.to_datetime(orders["signal_date"]) < pd.to_datetime(orders["execution_date"])).all()
    )


def _write_terminal_documents(
    experiment_dir: Path,
    *,
    execution_commit: str,
    status: str,
    research_pass_count: int,
    er_threshold: float,
    access_2026: bool,
    formal_pass: bool | None,
    baseline_metrics: Mapping[str, object],
    challenger_metrics: Mapping[str, object] | None,
) -> None:
    lines = [
        "# 0901_EX19 执行过程",
        "",
        f"- 执行提交：`{execution_commit}`。",
        "- 研究阶段完整评估625套预注册regime组权重。",
        f"- ER60研究样本中位数分界：`{er_threshold:.12f}`。",
        f"- 研究集合格候选：{research_pass_count}。",
        f"- 是否访问锁定2026测试：`{str(access_2026).lower()}`。",
        "- 因子、阈值、状态机、费用和次日开盘执行保持活动基线不变。",
    ]
    (experiment_dir / "03_execution.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    conclusion = [
        "# 0901_EX19 结论",
        "",
        f"状态：`{status}`。",
        "",
        f"研究基线最大回撤/卡玛/盈亏比：{float(baseline_metrics['max_drawdown']):.6f} / {float(baseline_metrics['calmar']):.6f} / {float(baseline_metrics['win_loss_ratio']):.6f}。",
    ]
    if challenger_metrics is None:
        conclusion.extend(
            [
                "没有候选通过研究集三指标与年度稳定性门槛，因此没有冻结挑战者，也没有读取2026。",
                "这否定的是本轮ER60中位数两档、组乘数网格和固定阈值架构，不外推为所有regime权重无效。",
            ]
        )
    else:
        conclusion.extend(
            [
                f"锁定测试挑战者最大回撤/卡玛/盈亏比：{float(challenger_metrics['max_drawdown']):.6f} / {float(challenger_metrics['calmar']):.6f} / {float(challenger_metrics['win_loss_ratio']):.6f}。",
                f"三指标、交易支持和审计联合PASS：`{str(bool(formal_pass)).lower()}`。",
                "收益率和夏普率仅客观报告，不参与判定；失败后未二次搜索。",
            ]
        )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(conclusion) + "\n", encoding="utf-8"
    )


def run_regime_weight_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    """Execute research selection, freeze, and conditional locked-2026 test."""
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_regime_weight_protocol(protocol)
    baseline = resolve_baseline(
        Path(baseline_root),
        str(protocol["baseline"]["version"]),
        symbol=str(protocol["symbol"]),
    )
    if baseline.sha256 != str(protocol["baseline"]["sha256"]):
        raise ValueError("regime-weight baseline registry identity differs")
    if float(baseline.rule.enter) != float(protocol["entry_threshold"]) or float(
        baseline.rule.exit
    ) != float(protocol["exit_threshold"]):
        raise ValueError("regime-weight baseline thresholds differ")

    research_data = load_market_data(
        raw_dir,
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff=pd.Timestamp(str(protocol["research_end"])),
    )
    factors, base_weights, champion = _strategy_inputs(research_data, baseline)
    groups = signal_groups(factors.columns)
    daily = _daily_prices(research_data)
    er = lagged_efficiency_ratio(daily["close"], int(protocol["er_lookback"]))
    threshold = fit_regime_threshold(
        er,
        pd.Timestamp(str(protocol["calibration_start"])),
        pd.Timestamp(str(protocol["research_end"])),
    )
    regimes = classify_regimes(er, threshold).reindex(factors.index)
    if regimes.isna().any():
        raise AssertionError("research regimes do not align to factors")
    support = (
        pd.DataFrame({"regime": regimes.astype(str)})
        .assign(year=lambda frame: frame.index.year)
        .groupby(["year", "regime"], as_index=False)
        .size()
    )
    support.to_csv(artifacts / "regime_support.csv", index=False, encoding="utf-8-sig")

    periods = _periods(
        str(protocol["research_start"]), str(protocol["research_end"]), include_years=True
    )
    fee_rate = float(protocol["fee_rate"])
    init_cash = float(protocol["init_cash"])
    baseline_results = run_period_backtests(
        research_data.daily,
        champion.target_position,
        periods,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    baseline_all = {name: _metrics(result, init_cash) for name, result in baseline_results.items()}
    base_main = baseline_all["continuous"]
    if not np.isfinite(
        [float(base_main[key]) for key in ("max_drawdown", "calmar", "win_loss_ratio")]
    ).all():
        raise ValueError("research baseline quality metrics are invalid")
    _write_json(artifacts / "baseline_metrics.json", baseline_all)

    targets: dict[int, pd.Series] = {}
    scores_by_id: dict[int, pd.Series] = {}
    weights_by_id: dict[int, dict[str, pd.Series]] = {}
    result_by_id: dict[int, dict[str, PeriodBacktestResult]] = {}
    annual_rows: list[dict[str, object]] = []
    candidate_rows: list[dict[str, object]] = []
    continuous_period = {"continuous": periods["continuous"]}
    annual_periods = {name: value for name, value in periods.items() if name != "continuous"}
    tolerance = float(protocol["tolerance"])
    for candidate_id, multipliers in enumerate(candidate_grid(protocol["multipliers"])):
        selected_weights = _weights_for_candidate(base_weights, groups, multipliers)
        scores = score_with_regime_weights(factors, regimes, selected_weights, base_weights)
        target = positions_from_scores(
            scores, baseline.rule.enter, baseline.rule.exit, baseline.rule
        )
        continuous_result = run_period_backtests(
            research_data.daily,
            target,
            continuous_period,
            fee_rate=fee_rate,
            init_cash=init_cash,
        )["continuous"]
        main = _metrics(continuous_result, init_cash)
        continuous_quality = strict_quality_pass(
            base_main,
            main,
            int(protocol["research_min_closed_trades"]),
            tolerance,
        )
        annual_double_wins = 0
        all_results = {"continuous": continuous_result}
        if continuous_quality:
            year_results = run_period_backtests(
                research_data.daily,
                target,
                annual_periods,
                fee_rate=fee_rate,
                init_cash=init_cash,
            )
            all_results.update(year_results)
            year_metrics = {
                name: _metrics(result, init_cash) for name, result in year_results.items()
            }
            annual_double_wins = _annual_double_wins(
                baseline_all, year_metrics, tolerance
            )
            for year, values in year_metrics.items():
                annual_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "year": year,
                        **values,
                        "baseline_max_drawdown": baseline_all[year]["max_drawdown"],
                        "baseline_calmar": baseline_all[year]["calmar"],
                    }
                )
        passed = research_candidate_passes(
            base_main,
            main,
            annual_double_wins,
            minimum_closed_trades=int(protocol["research_min_closed_trades"]),
            annual_minimum=int(protocol["annual_double_win_minimum"]),
            tolerance=tolerance,
        )
        relative = (
            candidate_relative_improvements(base_main, main)
            if continuous_quality
            else {"max_drawdown": float("-inf"), "calmar": float("-inf"), "win_loss_ratio": float("-inf")}
        )
        row = {
            "candidate_id": candidate_id,
            "trend_trend_multiplier": multipliers[0],
            "trend_volume_multiplier": multipliers[1],
            "range_trend_multiplier": multipliers[2],
            "range_volume_multiplier": multipliers[3],
            "continuous_quality_pass": continuous_quality,
            "annual_double_wins": annual_double_wins,
            "pass": passed,
            "maximin_improvement": min(relative.values()),
            "drawdown_relative_improvement": relative["max_drawdown"],
            "calmar_relative_improvement": relative["calmar"],
            "win_loss_relative_improvement": relative["win_loss_ratio"],
            "weight_shift": _weight_shift(base_weights, selected_weights),
            **main,
        }
        candidate_rows.append(row)
        if passed:
            targets[candidate_id] = target
            scores_by_id[candidate_id] = scores
            weights_by_id[candidate_id] = selected_weights
            result_by_id[candidate_id] = all_results

    candidates = rank_research_candidates(pd.DataFrame(candidate_rows))
    candidates.replace([np.inf, -np.inf], np.nan).to_csv(
        artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(annual_rows).to_csv(
        artifacts / "annual_metrics.csv", index=False, encoding="utf-8-sig"
    )
    passing = candidates.loc[candidates["pass"].astype(bool)]
    _write_json(
        artifacts / "candidate_funnel.json",
        {
            "candidate_count": len(candidates),
            "continuous_quality_pass_count": int(candidates["continuous_quality_pass"].sum()),
            "research_pass_count": int(len(passing)),
            "access_2026": False,
        },
    )

    if passing.empty:
        summary = {
            "status": "FAIL",
            "research_pass_count": 0,
            "er_threshold": threshold,
            "research_baseline": base_main,
            "access_2026": False,
        }
        _write_json(artifacts / "metrics.json", summary)
        _write_json(
            artifacts / "identity_audit.json",
            {
                "status": "PASS",
                "baseline_version": baseline.version,
                "baseline_sha256": baseline.sha256,
                "visible_data_hashes": research_data.hashes,
                "candidate_count": 625,
                "access_2026": False,
            },
        )
        _write_terminal_documents(
            experiment_dir,
            execution_commit=execution_commit,
            status="FAIL",
            research_pass_count=0,
            er_threshold=threshold,
            access_2026=False,
            formal_pass=None,
            baseline_metrics=base_main,
            challenger_metrics=None,
        )
        build_experiment_manifest(
            experiment_dir,
            {
                "experiment_id": "0901_EX19",
                "date": "2026-09-01",
                "status": "FAIL",
                "symbol": "588080.SH",
                "asset_type": "etf",
                "baseline": protocol["baseline"],
                "protocol_sha256": sha256((artifacts / "protocol.json").read_bytes()).hexdigest(),
                "visible_sample_end": "2025-12-31",
                "locked_test_accessed": False,
            },
        )
        validate_experiment_archive(experiment_dir)
        return {**summary, "experiment_dir": str(experiment_dir)}

    selected_id = int(passing.iloc[0]["candidate_id"])
    selected_row = passing.iloc[0]
    multipliers = (
        float(selected_row["trend_trend_multiplier"]),
        float(selected_row["trend_volume_multiplier"]),
        float(selected_row["range_trend_multiplier"]),
        float(selected_row["range_volume_multiplier"]),
    )
    selected_weights = weights_by_id[selected_id]
    frozen_payload = {
        "schema_version": 1,
        "experiment": "0901_EX19",
        "research_end": "2025-12-31",
        "baseline": protocol["baseline"],
        "er_lookback": 60,
        "er_threshold": threshold,
        "regime_labels": ["trend", "range", "warmup"],
        "multipliers": {
            "trend": {"trend": multipliers[0], "volume_position": multipliers[1]},
            "range": {"trend": multipliers[2], "volume_position": multipliers[3]},
        },
        "weights": {
            label: {str(name): float(value) for name, value in values.items()}
            for label, values in selected_weights.items()
        },
        "factor_names": list(map(str, baseline.factor_names)),
        "entry_threshold": float(baseline.rule.enter),
        "exit_threshold": float(baseline.rule.exit),
        "confirm_days": int(baseline.rule.confirm_days),
        "min_hold_days": int(baseline.rule.min_hold_days),
        "exit_confirm_days": int(baseline.rule.exit_confirm_days),
        "fee_rate": fee_rate,
        "execution": "next_session_open",
        "candidate_id": selected_id,
    }
    frozen_path = artifacts / "frozen_challenger.json"
    _write_json(frozen_path, frozen_payload)
    frozen_hash = sha256(frozen_path.read_bytes()).hexdigest()

    test_data = load_market_data(
        raw_dir,
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff=pd.Timestamp(str(protocol["test_end"])),
    )
    test_factors, test_base_weights, test_champion = _strategy_inputs(test_data, baseline)
    test_daily = _daily_prices(test_data)
    test_er = lagged_efficiency_ratio(test_daily["close"], 60)
    test_regimes = classify_regimes(test_er, threshold).reindex(test_factors.index)
    replay_weights = {
        label: pd.Series(values, index=test_base_weights.index, dtype=float)
        for label, values in frozen_payload["weights"].items()
    }
    test_scores = score_with_regime_weights(
        test_factors, test_regimes, replay_weights, test_base_weights
    )
    test_target = positions_from_scores(
        test_scores, baseline.rule.enter, baseline.rule.exit, baseline.rule
    )
    test_periods = {
        "2026_TEST": (pd.Timestamp(str(protocol["test_start"])), pd.Timestamp(str(protocol["test_end"]))),
        "2026Q1": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-31")),
        "2026H1": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-30")),
        "2026M1_M8": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-08-28")),
    }
    baseline_test_results = run_period_backtests(
        test_data.daily, test_champion.target_position, test_periods, fee_rate=fee_rate, init_cash=init_cash
    )
    challenger_test_results = run_period_backtests(
        test_data.daily, test_target, test_periods, fee_rate=fee_rate, init_cash=init_cash
    )
    baseline_test = {name: _metrics(result, init_cash) for name, result in baseline_test_results.items()}
    challenger_test = {name: _metrics(result, init_cash) for name, result in challenger_test_results.items()}
    formal_pass = strict_quality_pass(
        baseline_test["2026_TEST"],
        challenger_test["2026_TEST"],
        int(protocol["test_min_closed_trades"]),
        tolerance,
    )

    prefix_data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp("2023-12-31")
    )
    prefix_factors, prefix_base_weights, _ = _strategy_inputs(prefix_data, baseline)
    prefix_daily = _daily_prices(prefix_data)
    prefix_regimes = classify_regimes(
        lagged_efficiency_ratio(prefix_daily["close"], 60), threshold
    ).reindex(prefix_factors.index)
    prefix_scores = score_with_regime_weights(
        prefix_factors, prefix_regimes, selected_weights, prefix_base_weights
    )
    prefix_target = positions_from_scores(
        prefix_scores, baseline.rule.enter, baseline.rule.exit, baseline.rule
    )
    research_target = targets[selected_id]
    prefix_equal = prefix_target.equals(research_target.reindex(prefix_target.index))
    next_open = _next_open_audit(challenger_test_results["2026_TEST"].orders)
    causal_status = "PASS" if prefix_equal and next_open else "FAIL"
    if causal_status != "PASS":
        formal_status = "ERROR"
    else:
        formal_status = "PASS" if formal_pass else "FAIL"

    comparison_rows: list[dict[str, object]] = []
    for name in test_periods:
        left, right = baseline_test[name], challenger_test[name]
        comparison_rows.append(
            {
                "period": name,
                **{f"baseline_{key}": value for key, value in left.items()},
                **{f"challenger_{key}": value for key, value in right.items()},
            }
        )
    pd.DataFrame(comparison_rows).to_csv(
        artifacts / "period_comparison.csv", index=False, encoding="utf-8-sig"
    )
    formal_orders = challenger_test_results["2026_TEST"].orders
    formal_orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    closed_trade_ledger(formal_orders).to_csv(
        artifacts / "closed_trades.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(
        {
            "signal_date": test_target.index,
            "regime": test_regimes.astype(str).to_numpy(),
            "score": test_scores.to_numpy(),
            "target_position": test_target.to_numpy(),
        }
    ).to_csv(artifacts / "regime_weight_path.csv", index=False, encoding="utf-8-sig")
    _write_json(
        artifacts / "causal_replay_audit.json",
        {
            "status": causal_status,
            "prefix_cutoff": "2023-12-31",
            "prefix_target_equal": prefix_equal,
            "all_orders_signal_before_execution": next_open,
            "frozen_er_threshold_reused": True,
            "execution": "next_session_open",
        },
    )
    _write_json(
        artifacts / "identity_audit.json",
        {
            "status": "PASS",
            "baseline_version": baseline.version,
            "baseline_sha256": baseline.sha256,
            "frozen_challenger_sha256": frozen_hash,
            "research_data_hashes": research_data.hashes,
            "test_data_hashes": test_data.hashes,
            "candidate_count": 625,
            "selected_candidate_id": selected_id,
            "access_2026": True,
        },
    )
    summary = {
        "status": formal_status,
        "research_pass_count": int(len(passing)),
        "selected_candidate_id": selected_id,
        "er_threshold": threshold,
        "frozen_challenger_sha256": frozen_hash,
        "research_baseline": base_main,
        "research_challenger": _metrics(result_by_id[selected_id]["continuous"], init_cash),
        "test_baseline": baseline_test["2026_TEST"],
        "test_challenger": challenger_test["2026_TEST"],
        "formal_pass": formal_pass,
        "causal_audit": causal_status,
        "access_2026": True,
    }
    _write_json(artifacts / "test_metrics.json", {"baseline": baseline_test, "challenger": challenger_test})
    _write_json(artifacts / "metrics.json", summary)
    _write_terminal_documents(
        experiment_dir,
        execution_commit=execution_commit,
        status=formal_status,
        research_pass_count=int(len(passing)),
        er_threshold=threshold,
        access_2026=True,
        formal_pass=formal_pass and causal_status == "PASS",
        baseline_metrics=baseline_test["2026_TEST"],
        challenger_metrics=challenger_test["2026_TEST"],
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": "0901_EX19",
            "date": "2026-09-01",
            "status": formal_status,
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": protocol["baseline"],
            "protocol_sha256": sha256((artifacts / "protocol.json").read_bytes()).hexdigest(),
            "visible_sample_end": "2025-12-31",
            "locked_test_end": "2026-08-28",
            "locked_test_accessed": True,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
