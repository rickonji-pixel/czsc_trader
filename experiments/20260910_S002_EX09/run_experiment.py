from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.research_backtest import run_backtest
from czsc_trader.strategy_metrics import (
    closed_trade_ledger,
    strategy_comparison_metrics,
)
from strategy_evaluator import (
    ReturnMatrixEvidence,
    annualized_sharpe,
    calculate_dsr_bundle,
    cscv_pbo,
    effective_trial_count,
    hash_return_matrix,
    paired_stationary_bootstrap,
    performance_metrics,
    stationary_bootstrap_performance,
)


EXPERIMENT_ID = "20260910_S002_EX09"
CONTINUOUS_WINDOW = "2021_2026YTD"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_protocol(protocol: dict[str, object]) -> None:
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "automatic_acceptance",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("statistical audit may not select, promote, or deploy")
    trials = protocol["trial_universe"]["trials"]
    if len(trials) != int(protocol["trial_universe"]["raw_trial_count"]):
        raise ValueError("trial ledger count differs from frozen protocol")
    if protocol["trial_universe"]["selected_behavior"] != "CORE-H5":
        raise ValueError("selected behavior differs from frozen protocol")


def _validate_sources(repo_root: Path, protocol: dict[str, object]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for source_id, spec in protocol["source_experiments"].items():
        source = repo_root / "experiments" / str(spec["experiment_id"])
        validate_experiment_archive(source)
        expected = {source / "experiment_manifest.json": str(spec["manifest_sha256"])}
        if source_id in {"EX05", "EX06", "EX07"}:
            expected.update({
                source / "artifacts" / "prototype_daily.csv": str(
                    spec["prototype_daily_sha256"]
                ),
                source / "artifacts" / "window_metrics.csv": str(
                    spec["window_metrics_sha256"]
                ),
            })
        else:
            expected[source / "artifacts" / "identifiability_summary.json"] = str(
                spec["identifiability_summary_sha256"]
            )
            summary = _read_json(source / "artifacts" / "identifiability_summary.json")
            if summary["evidence_label"] != spec["required_evidence_label"]:
                raise ValueError("EX08 evidence label differs from frozen protocol")
        for path, digest in expected.items():
            if _sha256(path) != digest:
                raise ValueError(f"source evidence hash differs: {path}")
        paths[source_id] = source
    se = protocol["se_implementation"]
    expected_se = {
        repo_root / "packages" / "strategy_evaluator" / "src" / "strategy_evaluator"
        / "bootstrap.py": str(se["bootstrap_sha256"]),
        repo_root / "packages" / "strategy_evaluator" / "src" / "strategy_evaluator"
        / "search_bias.py": str(se["search_bias_sha256"]),
    }
    for path, digest in expected_se.items():
        if _sha256(path) != digest:
            raise ValueError(f"SE implementation differs from frozen protocol: {path}")
    return paths


def _prices(replay_data, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    prices = replay_data.execution_daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index).normalize(), name="dt")
    prices = prices.sort_index().loc[start:end]
    if prices.empty or prices.index[0] != start or prices.index[-1] != end:
        raise ValueError("evaluation prices do not match frozen trading sessions")
    return prices


def _target_from_trial(source: Path, trial: dict[str, object]) -> pd.Series:
    daily = pd.read_csv(source / "artifacts" / "prototype_daily.csv")
    field = str(trial["selector_field"])
    selector = trial["selector"]
    if field == "holding_sessions":
        selected = daily.loc[daily[field].astype(int).eq(int(selector))].copy()
    else:
        selected = daily.loc[daily[field].astype(str).eq(str(selector))].copy()
    if selected.empty:
        raise AssertionError(f"trial target missing: {trial['trial_id']}")
    selected["date"] = pd.to_datetime(selected["date"])
    target = selected.set_index("date")["target_position"].astype(float).sort_index()
    target.index = pd.DatetimeIndex(target.index, name="dt")
    if target.index.has_duplicates:
        raise AssertionError(f"trial target dates are duplicated: {trial['trial_id']}")
    return target


def _expected_metrics(
    source: Path,
    trial: dict[str, object],
    metric_selector_fields: dict[str, str],
) -> pd.Series:
    frame = pd.read_csv(source / "artifacts" / "window_metrics.csv")
    field = str(metric_selector_fields[str(trial["source"])])
    selector = trial["selector"]
    if field == "holding_sessions":
        selected = frame.loc[
            frame[field].astype(int).eq(int(selector))
            & frame["window"].eq(CONTINUOUS_WINDOW)
        ]
    else:
        selected = frame.loc[
            frame[field].astype(str).eq(str(selector))
            & frame["window"].eq(CONTINUOUS_WINDOW)
        ]
    if len(selected) != 1:
        raise AssertionError(f"source metrics missing: {trial['trial_id']}")
    return selected.iloc[0]


def _behavior_hash(target: pd.Series) -> str:
    return _canonical_hash({
        "dates": [value.strftime("%Y-%m-%d") for value in target.index],
        "target_position": target.astype(float).tolist(),
    })


def _replay_trials(
    prices: pd.DataFrame,
    sources: dict[str, Path],
    protocol: dict[str, object],
) -> tuple[dict[str, dict[str, object]], pd.DataFrame, pd.DataFrame]:
    initial_cash = float(protocol["shared_rules"]["initial_cash"])
    fee_rate = float(protocol["shared_rules"]["fee_rate_one_way"])
    behaviors: dict[str, dict[str, object]] = {}
    trial_rows: list[dict[str, object]] = []
    reproduction_rows: list[dict[str, object]] = []
    for trial in protocol["trial_universe"]["trials"]:
        target = _target_from_trial(sources[str(trial["source"])], trial).reindex(prices.index)
        if target.isna().any():
            raise AssertionError(f"trial target does not cover evaluation window: {trial['trial_id']}")
        digest = _behavior_hash(target)
        behavior_id = str(trial["behavior_id"])
        if behavior_id in behaviors and behaviors[behavior_id]["behavior_hash"] != digest:
            raise AssertionError(f"one behavior ID maps to different targets: {behavior_id}")
        result = run_backtest(
            prices,
            target,
            fee_rate=fee_rate,
            init_cash=initial_cash,
            initial_target=0.0,
        )
        metrics = strategy_comparison_metrics(result.equity, result.orders, initial_cash)
        trades = closed_trade_ledger(result.orders)
        expected = _expected_metrics(
            sources[str(trial["source"])],
            trial,
            protocol["trial_universe"]["metric_selector_fields"],
        )
        comparisons = {
            "max_drawdown": float(metrics["max_drawdown"]),
            "calmar": float(metrics["calmar"]),
            "win_loss_ratio": float(metrics["win_loss_ratio"]),
            "return": float(metrics["return"]),
            "sharpe": float(metrics["sharpe"]),
            "closed_trades": float(len(trades)),
        }
        for metric, actual in comparisons.items():
            if not np.isclose(actual, float(expected[metric]), rtol=1e-10, atol=1e-10):
                raise AssertionError(
                    f"{trial['trial_id']} {metric} differs: {actual}!={expected[metric]}"
                )
        returns = result.equity.pct_change().fillna(0.0).astype(float)
        behaviors.setdefault(behavior_id, {
            "behavior_hash": digest,
            "target": target,
            "returns": returns,
            "metrics": metrics,
            "orders": result.orders,
        })
        trial_rows.append({
            "trial_id": trial["trial_id"],
            "source": trial["source"],
            "selector": trial["selector"],
            "behavior_id": behavior_id,
            "behavior_hash": digest,
        })
        reproduction_rows.append({
            "trial_id": trial["trial_id"],
            "behavior_id": behavior_id,
            "source_metrics_reproduced": True,
            **comparisons,
        })
    if len(behaviors) != 9:
        raise AssertionError(f"expected 9 unique behaviors, found {len(behaviors)}")
    return behaviors, pd.DataFrame(trial_rows), pd.DataFrame(reproduction_rows)


def _return_evidence(
    dates: pd.DatetimeIndex,
    behaviors: dict[str, dict[str, object]],
) -> ReturnMatrixEvidence:
    ids = tuple(sorted(behaviors))
    matrix = np.column_stack([behaviors[item]["returns"].to_numpy() for item in ids])
    evidence = ReturnMatrixEvidence(
        tuple(value.strftime("%Y-%m-%d") for value in dates),
        ids,
        tuple(tuple(float(value) for value in row) for row in matrix),
        "",
    )
    return ReturnMatrixEvidence(
        evidence.dates,
        evidence.candidate_ids,
        evidence.returns,
        hash_return_matrix(evidence),
    )


def _absolute_audit(
    selected: np.ndarray,
    protocol: dict[str, object],
) -> tuple[list[dict[str, object]], list[object]]:
    settings = protocol["statistical_audit"]
    rows: list[dict[str, object]] = []
    reports = []
    for block in settings["bootstrap_block_lengths"]:
        report = stationary_bootstrap_performance(
            selected,
            candidate_id="CORE-H5",
            repetitions=int(settings["bootstrap_repetitions"]),
            mean_block_length=int(block),
            seed=int(settings["seed"]) + int(block),
        )
        reports.append(report)
        for metric in (report.cagr, report.max_drawdown, report.calmar):
            rows.append({
                "mean_block_length": int(block),
                **asdict(metric),
            })
    return rows, reports


def _pairwise_audit(
    behaviors: dict[str, dict[str, object]],
    protocol: dict[str, object],
) -> tuple[list[dict[str, object]], list[object]]:
    settings = protocol["statistical_audit"]
    selected = behaviors["CORE-H5"]["returns"].to_numpy()
    rows: list[dict[str, object]] = []
    reports = []
    for peer_index, peer in enumerate(protocol["trial_universe"]["pairwise_neighbors"]):
        for block in settings["bootstrap_block_lengths"]:
            report = paired_stationary_bootstrap(
                selected,
                behaviors[str(peer)]["returns"].to_numpy(),
                champion_id="CORE-H5",
                comparator_id=str(peer),
                repetitions=int(settings["bootstrap_repetitions"]),
                mean_block_length=int(block),
                seed=int(settings["seed"]) + peer_index * 1009 + int(block),
            )
            reports.append(report)
            for metric in (report.cagr, report.max_drawdown, report.calmar):
                rows.append({
                    "comparator_id": peer,
                    "mean_block_length": int(block),
                    **asdict(metric),
                })
    return rows, reports


def _leave_one_year_out(
    prices: pd.DataFrame,
    behaviors: dict[str, dict[str, object]],
) -> pd.DataFrame:
    rows = []
    years = sorted(set(prices.index.year))
    for behavior_id in ("CORE-H4", "CORE-H5", "CORE-H6"):
        returns = behaviors[behavior_id]["returns"].to_numpy()
        for omitted_year in years:
            mask = prices.index.year != omitted_year
            values = returns[mask]
            metrics = performance_metrics(values)
            rows.append({
                "behavior_id": behavior_id,
                "omitted_year": int(omitted_year),
                "observations": int(mask.sum()),
                "cagr": metrics.cagr,
                "max_drawdown": metrics.max_drawdown,
                "calmar": metrics.calmar,
                "sharpe": annualized_sharpe(values),
            })
    return pd.DataFrame(rows)


def _run_cost(
    prices: pd.DataFrame,
    target: pd.Series,
    bp: int,
    initial_cash: float,
) -> dict[str, float]:
    result = run_backtest(
        prices,
        target,
        fee_rate=float(bp) / 10_000.0,
        init_cash=initial_cash,
        initial_target=0.0,
    )
    metrics = strategy_comparison_metrics(result.equity, result.orders, initial_cash)
    return {
        "max_drawdown": float(metrics["max_drawdown"]),
        "calmar": float(metrics["calmar"]),
        "win_loss_ratio": float(metrics["win_loss_ratio"]),
        "return": float(metrics["return"]),
        "sharpe": float(metrics["sharpe"]),
        "closed_trades": int(len(closed_trade_ledger(result.orders))),
    }


def _cost_audit(
    prices: pd.DataFrame,
    behaviors: dict[str, dict[str, object]],
    protocol: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, int | None]:
    settings = protocol["cost_audit"]
    initial_cash = float(protocol["shared_rules"]["initial_cash"])
    band_rows = []
    for behavior_id in ("CORE-H4", "CORE-H5", "CORE-H6"):
        for bp in settings["band_cost_bp_one_way"]:
            band_rows.append({
                "behavior_id": behavior_id,
                "cost_bp_one_way": int(bp),
                **_run_cost(
                    prices,
                    behaviors[behavior_id]["target"],
                    int(bp),
                    initial_cash,
                ),
            })
    tolerance_rows = []
    threshold: int | None = None
    for bp in range(
        int(settings["tolerance_min_bp_one_way"]),
        int(settings["tolerance_max_bp_one_way"]) + 1,
        int(settings["tolerance_step_bp"]),
    ):
        metrics = _run_cost(
            prices,
            behaviors["CORE-H5"]["target"],
            bp,
            initial_cash,
        )
        tolerance_rows.append({"cost_bp_one_way": bp, **metrics})
        if threshold is None and metrics["calmar"] < float(settings["calmar_floor"]):
            threshold = bp
    return pd.DataFrame(band_rows), pd.DataFrame(tolerance_rows), threshold


def _direction_flags(
    absolute_reports: list[object],
    pbo: float,
    effective_dsr_probability: float,
    loyo: pd.DataFrame,
    band_cost: pd.DataFrame,
    protocol: dict[str, object],
) -> tuple[dict[str, str], str, dict[str, object]]:
    rules = protocol["risk_label_rules"]
    absolute = next(item for item in absolute_reports if item.mean_block_length == 21)
    if absolute.cagr.lower_90 > 0.0 and absolute.calmar.lower_90 > 0.0:
        absolute_flag = "FAVORABLE"
    elif absolute.cagr.lower_90 <= 0.0 and absolute.calmar.lower_90 <= 0.0:
        absolute_flag = "WEAK"
    else:
        absolute_flag = "MIXED"

    if pbo < float(rules["pbo_favorable_below"]):
        pbo_flag = "FAVORABLE"
    elif pbo > float(rules["pbo_weak_above"]):
        pbo_flag = "WEAK"
    else:
        pbo_flag = "MIXED"

    if effective_dsr_probability >= float(rules["dsr_favorable_at_least"]):
        dsr_flag = "FAVORABLE"
    elif effective_dsr_probability < float(rules["dsr_weak_below"]):
        dsr_flag = "WEAK"
    else:
        dsr_flag = "MIXED"

    negative_counts = loyo.assign(negative=loyo["calmar"].le(0)).groupby(
        "behavior_id"
    )["negative"].sum()
    if int(negative_counts.max()) == 0:
        loyo_flag = "FAVORABLE"
    elif int(negative_counts.max()) >= 2:
        loyo_flag = "WEAK"
    else:
        loyo_flag = "MIXED"

    h5_cost = band_cost.loc[band_cost["behavior_id"].eq("CORE-H5")].set_index(
        "cost_bp_one_way"
    )
    if float(h5_cost.loc[int(rules["cost_favorable_bp"]), "calmar"]) >= 1.0:
        cost_flag = "FAVORABLE"
    elif float(h5_cost.loc[int(rules["cost_weak_bp"]), "calmar"]) < 1.0:
        cost_flag = "WEAK"
    else:
        cost_flag = "MIXED"

    flags = {
        "absolute_bootstrap": absolute_flag,
        "pbo": pbo_flag,
        "dsr": dsr_flag,
        "leave_one_year_out": loyo_flag,
        "cost_tolerance": cost_flag,
    }
    weak = list(flags.values()).count("WEAK")
    favorable = list(flags.values()).count("FAVORABLE")
    if weak >= int(rules["weak_direction_count"]):
        uncapped = "WEAK"
    elif weak == 0 and favorable >= int(rules["favorable_direction_count"]):
        uncapped = "FAVORABLE"
    else:
        uncapped = "MIXED"
    overall = "MIXED" if uncapped == "FAVORABLE" else uncapped
    return flags, overall, {
        "uncapped_label": uncapped,
        "prior_evidence_cap": rules["maximum_overall_label_from_prior"],
        "absolute_cagr_lower_90": absolute.cagr.lower_90,
        "absolute_calmar_lower_90": absolute.calmar.lower_90,
        "loyo_negative_counts": negative_counts.astype(int).to_dict(),
        "h5_calmar_at_10bp": float(h5_cost.loc[10, "calmar"]),
        "h5_calmar_at_15bp": float(h5_cost.loc[15, "calmar"]),
    }


def _text(value: object, *, percent: bool = False) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):.2%}" if percent else f"{float(value):.4f}"


def _render_conclusion(
    pbo_result,
    dsr,
    effective_count: float,
    absolute_reports: list[object],
    pairwise_reports: list[object],
    loyo: pd.DataFrame,
    band_cost: pd.DataFrame,
    threshold: int | None,
    flags: dict[str, str],
    overall: str,
    details: dict[str, object],
) -> str:
    absolute = next(item for item in absolute_reports if item.mean_block_length == 21)
    pairwise = [item for item in pairwise_reports if item.mean_block_length == 21]
    lines = [
        f"# {EXPERIMENT_ID} 结论",
        "",
        f"状态：COMPLETE。统计稳健性证据标签为`{overall}`。",
        "",
        "## 核心结果",
        "",
        f"实际实施11次策略试验，按每日目标仓位去重后得到{pbo_result.candidate_count}条"
        f"独立行为路径；收益相关矩阵的有效试验数为{effective_count:.2f}。CSCV/PBO为"
        f"{pbo_result.pbo:.2%}。五日核心的原始DSR概率为{dsr.raw.probability:.2%}，按有效"
        f"试验数修正后的DSR概率为{dsr.effective.probability:.2%}。",
        "",
        "21日平均区块Bootstrap下：",
        "",
        "| 指标 | 点估计 | 90%区间 | 95%区间 | 大于0概率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for metric in (absolute.cagr, absolute.max_drawdown, absolute.calmar):
        percent = metric.metric != "calmar"
        lines.append(
            f"| {metric.metric} | {_text(metric.point, percent=percent)} | "
            f"[{_text(metric.lower_90, percent=percent)}, {_text(metric.upper_90, percent=percent)}] | "
            f"[{_text(metric.lower_95, percent=percent)}, {_text(metric.upper_95, percent=percent)}] | "
            f"{_text(metric.probability_above_zero, percent=True)} |"
        )
    lines.extend([
        "",
        "## 五日与机制邻居",
        "",
        "| 对照 | CAGR胜出概率 | 最大回撤胜出概率 | 卡玛胜出概率 |",
        "|---|---:|---:|---:|",
    ])
    for item in pairwise:
        lines.append(
            f"| {item.comparator_id} | {_text(item.cagr.probability_favorable, percent=True)} | "
            f"{_text(item.max_drawdown.probability_favorable, percent=True)} | "
            f"{_text(item.calmar.probability_favorable, percent=True)} |"
        )
    negative = loyo.assign(negative=loyo["calmar"].le(0)).groupby("behavior_id")[
        "negative"
    ].sum()
    cost_15 = band_cost.loc[band_cost["cost_bp_one_way"].eq(15)].set_index("behavior_id")
    lines.extend([
        "",
        "## 年度删除与成本",
        "",
        "四、五、六日逐年留一后的负卡玛场景数分别为"
        f"{int(negative['CORE-H4'])}、{int(negative['CORE-H5'])}、{int(negative['CORE-H6'])}。"
        "单边15 bp全包成本下，三者卡玛分别为"
        f"{_text(cost_15.loc['CORE-H4', 'calmar'])}、"
        f"{_text(cost_15.loc['CORE-H5', 'calmar'])}、"
        f"{_text(cost_15.loc['CORE-H6', 'calmar'])}。",
        "五日卡玛首次跌破1的扫描成本为"
        + (f"单边{threshold} bp。" if threshold is not None else "单边100 bp以上。"),
        "",
        "## 方向标签",
        "",
        "| 审计方向 | 标签 |",
        "|---|---|",
    ])
    labels = {
        "absolute_bootstrap": "绝对Bootstrap",
        "pbo": "PBO",
        "dsr": "DSR",
        "leave_one_year_out": "逐年留一",
        "cost_tolerance": "成本容忍度",
    }
    for key, value in flags.items():
        lines.append(f"| {labels[key]} | `{value}` |")
    lines.extend([
        "",
        "## 裁决",
        "",
    ])
    if overall == "WEAK":
        lines.append("至少两个统计方向给出弱证据，停止当前核心，不进入S002候选评审。")
    else:
        lines.append(
            "统计结果没有达到停止条件，可以进入首个S002候选的人工评审。受EX08匹配近邻池"
            f"不足限制，未经上限约束的标签为`{details['uncapped_label']}`，最终仍封顶为"
            "`MIXED`；本轮没有自动创建候选。"
        )
    lines.extend([
        "",
        "## 边界",
        "",
        "PBO和DSR只覆盖11次已完整实施的试验，是搜索偏差的下界；662个信号配置和人工假设"
        "收敛产生的研究者自由度没有被等价计入。全部结果仍来自同一开发池，不构成样本外证明。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate_protocol(protocol)
    sources = _validate_sources(repo_root, protocol)

    target = protocol["research_target"]
    start = pd.Timestamp(target["evaluation_start"])
    end = pd.Timestamp(target["development_cutoff"])
    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    replay_data = load_replay_data(
        context,
        "research",
        str(target["symbol"]),
        str(target["asset_type"]),
        end.date(),
    )
    prices = _prices(replay_data, start, end)
    behaviors, trial_ledger, reproduction = _replay_trials(prices, sources, protocol)
    evidence = _return_evidence(prices.index, behaviors)
    matrix = np.asarray(evidence.returns, dtype=float)
    pbo_result = cscv_pbo(evidence, int(protocol["statistical_audit"]["cscv_block_count"]))
    sharpes = np.asarray([annualized_sharpe(matrix[:, index]) for index in range(matrix.shape[1])])
    effective_count = effective_trial_count(matrix)
    selected_index = evidence.candidate_ids.index("CORE-H5")
    dsr = calculate_dsr_bundle(
        matrix[:, selected_index],
        sharpes,
        raw_count=int(protocol["trial_universe"]["raw_trial_count"]),
        effective_count=effective_count,
    )
    absolute_rows, absolute_reports = _absolute_audit(matrix[:, selected_index], protocol)
    pairwise_rows, pairwise_reports = _pairwise_audit(behaviors, protocol)
    loyo = _leave_one_year_out(prices, behaviors)
    band_cost, tolerance, threshold = _cost_audit(prices, behaviors, protocol)
    flags, overall, label_details = _direction_flags(
        absolute_reports,
        pbo_result.pbo,
        dsr.effective.probability,
        loyo,
        band_cost,
        protocol,
    )

    _write_csv(trial_ledger, artifacts / "trial_ledger.csv")
    _write_csv(reproduction, artifacts / "reproduction_audit.csv")
    _write_csv(
        pd.DataFrame(matrix, columns=evidence.candidate_ids).assign(
            date=list(evidence.dates)
        )[["date", *evidence.candidate_ids]],
        artifacts / "daily_return_matrix.csv",
    )
    _write_json(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write_json(artifacts / "pbo.json", pbo_result.to_dict())
    _write_csv(
        pd.DataFrame([item.to_dict() for item in pbo_result.splits]),
        artifacts / "cscv_splits.csv",
    )
    _write_json(artifacts / "dsr.json", dsr.to_dict())
    _write_csv(pd.DataFrame(absolute_rows), artifacts / "absolute_bootstrap.csv")
    _write_csv(pd.DataFrame(pairwise_rows), artifacts / "pairwise_bootstrap.csv")
    _write_csv(loyo, artifacts / "leave_one_year_out.csv")
    _write_csv(band_cost, artifacts / "band_cost_stress.csv")
    _write_csv(tolerance, artifacts / "cost_tolerance_curve.csv")
    _write_json(artifacts / "statistical_summary.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "evidence_label": overall,
        "direction_flags": flags,
        "details": label_details,
        "raw_trial_count": len(trial_ledger),
        "unique_behavior_count": len(behaviors),
        "effective_trial_count": effective_count,
        "pbo": pbo_result.pbo,
        "dsr_raw_probability": dsr.raw.probability,
        "dsr_effective_probability": dsr.effective.probability,
        "h5_calmar_below_one_cost_bp_one_way": threshold,
    })
    output_names = [
        "trial_ledger.csv",
        "reproduction_audit.csv",
        "daily_return_matrix.csv",
        "return_matrix_evidence.json",
        "pbo.json",
        "cscv_splits.csv",
        "dsr.json",
        "absolute_bootstrap.csv",
        "pairwise_bootstrap.csv",
        "leave_one_year_out.csv",
        "band_cost_stress.csv",
        "cost_tolerance_curve.csv",
        "statistical_summary.json",
    ]
    _write_json(artifacts / "run_evidence.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset_fingerprint": replay_data.fingerprint,
        "cutoff": replay_data.cutoff.isoformat(),
        "evaluation_sessions": len(prices),
        "source_reproduction_passed": bool(reproduction["source_metrics_reproduced"].all()),
        "se_return_matrix_hash": evidence.content_hash,
        "evidence_label": overall,
        "outputs": {
            name: {
                "bytes": (artifacts / name).stat().st_size,
                "sha256": _sha256(artifacts / name),
            }
            for name in output_names
        },
    })
    (experiment_dir / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        f"11次实际试验的源指标全部复现，按每日目标仓位去重为{len(behaviors)}条行为路径。"
        f"完成SE绝对及配对固定区块Bootstrap、CSCV/PBO、原始与有效试验数DSR、逐年留一"
        f"和5至100 bp成本容忍度扫描。首次执行在统计输出前因EX05指标筛选列名错误而失败，"
        f"修复和无结果泄漏证据见`attempt_log.json`。\n\n证据标签：`{overall}`。未生成候选，也未调用SM"
        "或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(
            pbo_result,
            dsr,
            effective_count,
            absolute_reports,
            pairwise_reports,
            loyo,
            band_cost,
            threshold,
            flags,
            overall,
            label_details,
        ),
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": target["development_cutoff"],
            "evidence_label": overall,
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
