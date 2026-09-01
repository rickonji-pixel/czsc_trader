"""Reproduce and audit baseline_20260901 without changing the strategy."""

from __future__ import annotations

from hashlib import sha256
from itertools import combinations, product
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from czsc_trader.backtest import run_backtest, run_period_backtests
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.factors import generate_factor_frame, signal_groups
from czsc_trader.four_layer import normalized_signal_factors, positions_from_scores
from czsc_trader.regime_weight import classify_regimes, lagged_efficiency_ratio, project_group_weights, score_with_regime_weights
from czsc_trader.robustness import cscv_pbo, cyclic_shifts, deflated_sharpe_ratio, parameter_geometry
from czsc_trader.strategy_metrics import closed_trade_ledger, strategy_comparison_metrics


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT_DIR / "artifacts"
PROTOCOL_PATH = ARTIFACTS / "protocol.json"
EX20_DIR = REPO_ROOT / "experiments" / "0901_EX20"
BASELINE_ROOT = REPO_ROOT / "configs" / "rule_baselines"
RAW_DIR = REPO_ROOT / "data" / "raw"
PARAMETERS = (
    "trend_trend_multiplier",
    "trend_volume_multiplier",
    "range_trend_multiplier",
    "range_volume_multiplier",
)


def _json_default(value: object) -> object:
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _file_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, encoding="utf-8"
    ).strip()


def _protocol() -> dict[str, object]:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    expected = {
        "experiment_id": "0902_EX01",
        "status": "PRE_REGISTERED",
        "candidate_count": 625,
        "selected_candidate_id": 143,
        "cscv_block_count": 10,
        "cscv_split_count": 252,
        "placebo_end": "2026-09-01",
        "strategy_change_allowed": False,
        "market_data_update_allowed": False,
        "composite_pass_gate": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"protocol field {key} differs from preregistration")
    return payload


def _critical_hashes() -> dict[str, str]:
    paths = (
        REPO_ROOT / "configs" / "rule_baselines" / "registry.json",
        REPO_ROOT / "configs" / "rule_baselines" / "baseline_20260901.json",
        EX20_DIR / "artifacts" / "protocol.json",
        EX20_DIR / "artifacts" / "frozen_challenger.json",
        EX20_DIR / "artifacts" / "candidate_results.csv",
        REPO_ROOT / "data" / "raw" / "588080_manifest.json",
        REPO_ROOT / "data" / "raw" / "588080_validation.json",
        REPO_ROOT / "src" / "czsc_trader" / "backtest.py",
        REPO_ROOT / "src" / "czsc_trader" / "four_layer.py",
        REPO_ROOT / "src" / "czsc_trader" / "regime_weight.py",
    )
    return {path.relative_to(REPO_ROOT).as_posix(): _file_sha(path) for path in paths}


def _daily_prices(data: object) -> pd.DataFrame:
    daily = data.daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"])
    return daily.set_index("dt").sort_index()


def _equity_returns(equity: pd.Series, init_cash: float) -> pd.Series:
    values = equity.astype(float)
    returns = values.pct_change()
    returns.iloc[0] = values.iloc[0] / float(init_cash) - 1.0
    if not np.isfinite(returns.to_numpy()).all():
        raise ValueError("equity produced non-finite daily returns")
    return returns.rename("daily_return")


def _full_metrics(result: object, init_cash: float) -> dict[str, object]:
    comparison = strategy_comparison_metrics(
        result.equity, result.orders, init_cash, float(result.metrics["sharpe"])
    )
    ledger = closed_trade_ledger(result.orders)
    trade_returns = ledger["net_return"].astype(float) if not ledger.empty else pd.Series(dtype=float)
    total_return = float(result.equity.iloc[-1] / init_cash - 1.0)
    annualized_return = float((result.equity.iloc[-1] / init_cash) ** (252.0 / len(result.equity)) - 1.0)
    return {
        "strategy_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": comparison["max_drawdown"],
        "calmar": comparison["calmar"],
        "win_loss_ratio": comparison["win_loss_ratio"],
        "closed_trade_count": int(len(ledger)),
        "winning_trade_count": int(trade_returns.gt(0.0).sum()),
        "losing_trade_count": int(trade_returns.lt(0.0).sum()),
        "has_wins_and_losses": bool(trade_returns.gt(0.0).any() and trade_returns.lt(0.0).any()),
        "sharpe": comparison["sharpe"],
        "exposure": float(result.metrics["exposure"]),
        "trade_count": int(result.metrics["trade_count"]),
    }


def _candidate_grid(levels: list[float]) -> tuple[tuple[float, float, float, float], ...]:
    grid = tuple(product(map(float, levels), repeat=4))
    if len(grid) != 625 or grid[143] != (0.75, 0.5, 1.25, 1.25):
        raise AssertionError("EX20 candidate grid identity differs")
    return grid


def _candidate_weights(
    base_weights: pd.Series,
    groups: dict[str, tuple[str, ...]],
    values: tuple[float, float, float, float],
) -> dict[str, pd.Series]:
    return {
        "trend": project_group_weights(base_weights, groups, values[0], values[1]),
        "range": project_group_weights(base_weights, groups, values[2], values[3]),
    }


def reconstruct_candidate_paths(
    protocol: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object], dict[str, object]]:
    parent = resolve_baseline(BASELINE_ROOT, "baseline_20260826", symbol="588080.SH")
    if parent.sha256 != protocol["parent_baseline"]["sha256"]:
        raise ValueError("parent baseline identity differs")
    data = load_market_data(RAW_DIR, "588080.SH", "etf", cutoff=protocol["research_end"])
    factor_frame = generate_factor_frame(data).frame
    names = list(parent.factor_names)
    factors = normalized_signal_factors(factor_frame[names]).astype(float)
    base_weights = pd.Series(parent.factor_weights, index=names, name="weight", dtype=float)
    groups = signal_groups(factors.columns)
    daily = _daily_prices(data)
    frozen = json.loads((EX20_DIR / "artifacts" / "frozen_challenger.json").read_text(encoding="utf-8"))
    regimes = classify_regimes(
        lagged_efficiency_ratio(daily["close"], int(frozen["er_lookback"])),
        float(frozen["er_threshold"]),
    ).reindex(factors.index)
    if regimes.isna().any():
        raise ValueError("regime labels do not align to factor rows")

    start = pd.Timestamp(str(protocol["research_start"]))
    end = pd.Timestamp(str(protocol["research_end"]))
    period = {"research": (start, end)}
    fee = float(protocol["fee_rate"])
    cash = float(protocol["init_cash"])
    return_columns: dict[str, pd.Series] = {}
    metric_rows: list[dict[str, object]] = []
    started = time.perf_counter()
    for candidate_id, multipliers in enumerate(_candidate_grid(protocol["multipliers"])):
        weights = _candidate_weights(base_weights, groups, multipliers)
        scores = score_with_regime_weights(factors, regimes, weights, base_weights)
        target = positions_from_scores(scores, parent.rule.enter, parent.rule.exit, parent.rule)
        result = run_period_backtests(
            data.daily, target, period, fee_rate=fee, init_cash=cash
        )["research"]
        returns = _equity_returns(result.equity, cash)
        return_columns[str(candidate_id)] = returns
        metric_rows.append(
            {
                "candidate_id": candidate_id,
                **dict(zip(PARAMETERS, multipliers)),
                **_full_metrics(result, cash),
            }
        )
    returns = pd.DataFrame(return_columns)
    metrics = pd.DataFrame(metric_rows).sort_values("candidate_id").reset_index(drop=True)

    archived = pd.read_csv(EX20_DIR / "artifacts" / "candidate_results.csv")
    archived_row = archived.loc[archived["candidate_id"].eq(143)]
    rebuilt_row = metrics.loc[metrics["candidate_id"].eq(143)]
    if len(archived_row) != 1 or len(rebuilt_row) != 1 or set(archived["candidate_id"]) != set(metrics["candidate_id"]):
        raise ValueError("EX20 candidate identities did not reproduce")
    comparison_keys = (
        "strategy_return", "annualized_return", "max_drawdown", "calmar",
        "win_loss_ratio", "closed_trade_count", "sharpe", "exposure", "trade_count",
    )
    differences: dict[str, float] = {}
    for key in comparison_keys:
        old = float(archived_row.iloc[0][key])
        new = float(rebuilt_row.iloc[0][key])
        differences[key] = new - old
        if not np.isclose(new, old, rtol=0.0, atol=float(protocol["reproduction_tolerance"])):
            raise ValueError(f"candidate143 reproduction differs for {key}: {new} vs {old}")
    audit = {
        "status": "PASS",
        "candidate_count": int(len(metrics)),
        "return_observations": int(len(returns)),
        "start": str(returns.index.min().date()),
        "end": str(returns.index.max().date()),
        "candidate143_differences": differences,
        "elapsed_seconds": time.perf_counter() - started,
    }
    context = {
        "research_data_hashes": data.hashes,
        "frozen_er_threshold": float(frozen["er_threshold"]),
    }
    return returns, metrics, audit, context


def _write_parameter_chart(surface: pd.DataFrame, selected_id: int) -> None:
    selected = surface.loc[surface["candidate_id"].eq(selected_id)].iloc[0]
    pairs = list(combinations(PARAMETERS, 2))
    figure = make_subplots(rows=2, cols=3, subplot_titles=[f"{left} × {right}" for left, right in pairs])
    for position, (left, right) in enumerate(pairs):
        held = [name for name in PARAMETERS if name not in {left, right}]
        view = surface.copy()
        for name in held:
            view = view.loc[np.isclose(view[name], float(selected[name]))]
        pivot = view.pivot(index=right, columns=left, values="sharpe").sort_index().sort_index(axis=1)
        row, column = divmod(position, 3)
        figure.add_trace(
            go.Heatmap(
                x=pivot.columns,
                y=pivot.index,
                z=pivot.to_numpy(),
                coloraxis="coloraxis",
                hovertemplate=f"{left}=%{{x}}<br>{right}=%{{y}}<br>Sharpe=%{{z:.4f}}<extra></extra>",
            ),
            row=row + 1,
            col=column + 1,
        )
    figure.update_layout(
        title="EX20 625候选参数切片（其余参数固定为候选143）",
        coloraxis={"colorscale": "RdYlGn", "colorbar": {"title": "Sharpe"}},
        height=800,
        width=1500,
    )
    figure.write_html(ARTIFACTS / "parameter_slices.html", include_plotlyjs=True)


def _placebo_metrics(result: object, cash: float) -> dict[str, object]:
    return _full_metrics(result, cash)


def run_placebo_audit(protocol: dict[str, object]) -> tuple[pd.DataFrame, dict[str, object], dict[str, str]]:
    active = resolve_baseline(BASELINE_ROOT, "baseline_20260901", symbol="588080.SH")
    if active.sha256 != protocol["active_baseline"]["sha256"] or active.strategy != "czsc_regime_weight":
        raise ValueError("active baseline identity differs")
    data = load_market_data(RAW_DIR, "588080.SH", "etf", cutoff=protocol["placebo_end"])
    frame = generate_factor_frame(data).frame
    prices = _daily_prices(data)
    applied = apply_resolved_baseline(frame, active, daily_close=prices["close"])
    start = pd.Timestamp(str(protocol["placebo_start"]))
    end = pd.Timestamp(str(protocol["placebo_end"]))
    period_prices = prices.loc[start:end]
    target = applied.target_position.reindex(period_prices.index).astype(float)
    if target.isna().any() or len(target) < 2:
        raise ValueError("placebo target sequence is incomplete")
    cash = float(protocol["init_cash"])
    fee = float(protocol["fee_rate"])
    sequences = (target,) + cyclic_shifts(target)
    rows: list[dict[str, object]] = []
    for lag, shifted in enumerate(sequences):
        result = run_backtest(
            period_prices,
            shifted,
            fee_rate=fee,
            init_cash=cash,
            initial_target=float(shifted.iloc[-1]),
            initial_signal_date=shifted.index[-1],
        )
        rows.append({"lag": lag, "is_observed": lag == 0, **_placebo_metrics(result, cash)})
    details = pd.DataFrame(rows)
    observed = float(details.loc[details["lag"].eq(0), "sharpe"].iloc[0])
    placebo = details.loc[details["lag"].ne(0), "sharpe"].astype(float)
    exceedances = int(placebo.ge(observed).sum())
    pvalue = float((1 + exceedances) / (1 + len(placebo)))
    percentile = float(placebo.lt(observed).mean())
    summary = {
        "status": "PASS",
        "start": str(period_prices.index.min().date()),
        "end": str(period_prices.index.max().date()),
        "session_count": int(len(target)),
        "placebo_count": int(len(placebo)),
        "observed_sharpe": observed,
        "placebo_sharpe_mean": float(placebo.mean()),
        "placebo_sharpe_median": float(placebo.median()),
        "placebo_sharpe_max": float(placebo.max()),
        "exceedance_count": exceedances,
        "empirical_pvalue": pvalue,
        "observed_percentile": percentile,
        "boundary_convention": "circular predecessor supplies initial execution target",
    }
    return details, summary, data.hashes


def _find_numeric(payload: object, keys: tuple[str, ...]) -> tuple[int | None, str | None]:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)) and np.isfinite(value):
                return int(value), f"protocol:{key}"
        for value in payload.values():
            found, basis = _find_numeric(value, keys)
            if found is not None:
                return found, basis
    if isinstance(payload, list):
        for value in payload:
            found, basis = _find_numeric(value, keys)
            if found is not None:
                return found, basis
    return None, None


def build_trial_ledger() -> tuple[pd.DataFrame, dict[str, object]]:
    rows: list[dict[str, object]] = []
    for directory in sorted((REPO_ROOT / "experiments").iterdir()):
        if not directory.is_dir() or directory.name >= "0902_EX01":
            continue
        manifest_path = directory / "experiment_manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        protocol_path = directory / "artifacts" / "protocol.json"
        protocol = json.loads(protocol_path.read_text(encoding="utf-8")) if protocol_path.is_file() else {}
        count, basis = _find_numeric(
            protocol,
            ("candidate_count", "trial_count", "n_trials", "evaluated_candidate_count", "evaluated_count"),
        )
        if count is None:
            candidate_path = directory / "artifacts" / "candidate_results.csv"
            if candidate_path.is_file():
                count = len(pd.read_csv(candidate_path))
                basis = "artifact:candidate_results.csv rows"
        baseline = manifest.get("baseline", protocol.get("baseline"))
        if isinstance(baseline, dict):
            baseline_version = baseline.get("version")
        else:
            baseline_version = baseline
        rows.append(
            {
                "experiment_id": directory.name,
                "status": manifest.get("status"),
                "symbol": manifest.get("symbol", protocol.get("symbol")),
                "baseline_version": baseline_version,
                "visible_sample_end": manifest.get("visible_sample_end", protocol.get("research_end")),
                "locked_test_end": manifest.get("locked_test_end", protocol.get("test_end")),
                "locked_test_accessed": manifest.get("locked_test_accessed"),
                "known_trial_count": count,
                "count_basis": basis or "unknown",
                "current_baseline_lineage": (
                    "direct_source" if directory.name == "0901_EX20" else
                    "parent_baseline" if baseline_version == "baseline_20260826" else
                    "other_or_unknown"
                ),
            }
        )
    ledger = pd.DataFrame(rows)
    summary = {
        "experiment_count": int(len(ledger)),
        "known_trial_count_experiments": int(ledger["known_trial_count"].notna().sum()),
        "unknown_trial_count_experiments": int(ledger["known_trial_count"].isna().sum()),
        "direct_source_experiment": "0901_EX20",
        "heterogeneous_counts_combined_into_pbo_or_dsr": False,
    }
    return ledger, summary


def _classification(pbo: float, dsr: float, pvalue: float) -> dict[str, str]:
    pbo_text = "低" if pbo < 0.10 else "显著" if pbo < 0.50 else "高"
    dsr_text = "达到0.95参考线" if dsr >= 0.95 else "未达到0.95参考线"
    placebo_text = "达到0.05参考线" if pvalue <= 0.05 else "未达到0.05参考线"
    return {"pbo_risk_band": pbo_text, "dsr_reference": dsr_text, "placebo_reference": placebo_text}


def _write_documents(
    *, execution_commit: str, reproduction: dict[str, object], pbo: dict[str, object],
    dsr: dict[str, object], placebo: dict[str, object], ledger: dict[str, object],
    surface: pd.DataFrame, neighbors: pd.DataFrame, elapsed: float,
) -> None:
    selected = surface.loc[surface["candidate_id"].eq(143)].iloc[0]
    immediate = neighbors.loc[neighbors["manhattan_distance"].eq(1.0)]
    classifications = _classification(
        float(pbo["pbo"]), float(dsr["dsr_probability"]), float(placebo["empirical_pvalue"])
    )
    execution_lines = [
        "# 0902_EX01 执行过程", "",
        f"- 执行提交：`{execution_commit}`。",
        f"- EX20重建：625套候选、{reproduction['return_observations']}个共同交易日，状态PASS。",
        f"- CSCV：10个连续区块、{pbo['split_count']}个互补切分。",
        f"- 2026循环移位：{placebo['session_count']}个交易日、{placebo['placebo_count']}个非零位移。",
        f"- 历史试验账本：{ledger['experiment_count']}个实验，其中{ledger['unknown_trial_count_experiments']}个无法可靠恢复试验数。",
        f"- 总耗时：{elapsed:.2f}秒。",
        "- 运行期间未修改活动基线、协议或行情数据。",
    ]
    (EXPERIMENT_DIR / "03_execution.md").write_text("\n".join(execution_lines) + "\n", encoding="utf-8")
    conclusion_lines = [
        "# 0902_EX01 结论", "", "本实验不设置综合PASS。分项结论如下：", "",
        f"1. **搜索偏差**：PBO为`{float(pbo['pbo']):.4f}`，落在预注册的“{classifications['pbo_risk_band']}”风险解释带；验证集排名中位百分位为`{float(pbo['median_validation_percentile']):.4f}`。",
        f"2. **多重试验修正**：候选143观测夏普`{float(dsr['observed_sharpe']):.4f}`，625次试验对应的随机最大夏普基准`{float(dsr['expected_max_sharpe']):.4f}`，DSR概率`{float(dsr['dsr_probability']):.4f}`，{classifications['dsr_reference']}。",
        f"3. **参数形态**：候选143全局夏普`{float(selected['sharpe']):.4f}`；{len(immediate)}个一步邻居夏普中位数`{float(immediate['sharpe'].median()):.4f}`、最差`{float(immediate['sharpe'].min()):.4f}`。完整曲面和六组切片保留连续退化证据，不强行二分高原/尖峰。",
        f"4. **2026时序对齐**：真实夏普`{float(placebo['observed_sharpe']):.4f}`，在{placebo['placebo_count']}个合法错位中的经验分位数`{float(placebo['observed_percentile']):.4f}`，精确经验P值`{float(placebo['empirical_pvalue']):.4f}`，{classifications['placebo_reference']}。",
        f"5. **项目试验披露**：此前{ledger['experiment_count']}个实验中，{ledger['known_trial_count_experiments']}个可恢复试验数、{ledger['unknown_trial_count_experiments']}个为unknown；异质试验数未并入PBO或DSR。",
        "",
        "最强限制：588080单标的、有限年份和已被研究过程观察过的2026，仍不足以证明未来或实盘Alpha。最强价值：本次把搜索空间、参数邻域和时序错位的风险第一次用冻结协议量化，而没有反向修改策略。",
    ]
    (EXPERIMENT_DIR / "04_conclusion.md").write_text("\n".join(conclusion_lines) + "\n", encoding="utf-8")


def run() -> dict[str, object]:
    started = time.perf_counter()
    protocol = _protocol()
    before = _critical_hashes()
    execution_commit = _git_head()

    returns, metrics, reproduction, context = reconstruct_candidate_paths(protocol)
    np.savez_compressed(
        ARTIFACTS / "candidate_daily_returns.npz",
        values=returns.to_numpy(dtype=float),
        dates=returns.index.astype("datetime64[ns]").to_numpy(),
        candidate_ids=np.asarray(returns.columns, dtype=int),
    )
    _write_csv(
        ARTIFACTS / "candidate_return_index.csv",
        pd.DataFrame({"row": np.arange(len(returns)), "dt": returns.index}),
    )
    _write_csv(ARTIFACTS / "candidate_metrics.csv", metrics)
    _write_json(ARTIFACTS / "reproduction_audit.json", reproduction)

    cscv_details, pbo = cscv_pbo(returns, int(protocol["cscv_block_count"]))
    if len(cscv_details) != int(protocol["cscv_split_count"]):
        raise ValueError("CSCV split count differs from protocol")
    _write_csv(ARTIFACTS / "cscv_splits.csv", cscv_details)
    _write_json(ARTIFACTS / "pbo_summary.json", pbo)

    selected_returns = returns["143"]
    dsr = deflated_sharpe_ratio(selected_returns, metrics.set_index("candidate_id")["sharpe"])
    _write_json(ARTIFACTS / "deflated_sharpe.json", dsr)

    surface, neighbors = parameter_geometry(metrics, 143, PARAMETERS)
    for metric in ("strategy_return", "sharpe", "max_drawdown", "calmar", "win_loss_ratio"):
        surface[f"{metric}_global_percentile"] = surface[metric].rank(pct=True, method="average")
    _write_csv(ARTIFACTS / "parameter_surface.csv", surface)
    _write_csv(ARTIFACTS / "neighbor_geometry.csv", neighbors)
    _write_parameter_chart(surface, 143)

    placebo_details, placebo, test_hashes = run_placebo_audit(protocol)
    _write_csv(ARTIFACTS / "placebo_shifts.csv", placebo_details)
    _write_json(ARTIFACTS / "placebo_summary.json", placebo)

    ledger_frame, ledger = build_trial_ledger()
    if len(ledger_frame) != 46:
        raise ValueError(f"expected 46 preceding experiments, found {len(ledger_frame)}")
    _write_csv(ARTIFACTS / "trial_ledger.csv", ledger_frame)
    _write_json(ARTIFACTS / "trial_ledger_summary.json", ledger)

    after = _critical_hashes()
    identity = {
        "status": "PASS" if before == after else "FAIL",
        "active_baseline": protocol["active_baseline"],
        "source_experiment": protocol["source_experiment"],
        "critical_hashes_before": before,
        "critical_hashes_after": after,
        "critical_hashes_unchanged": before == after,
        "research_data_hashes": context["research_data_hashes"],
        "placebo_data_hashes": test_hashes,
        "execution_commit": execution_commit,
    }
    if before != after:
        raise ValueError("critical identities changed during execution")
    _write_json(ARTIFACTS / "identity_audit.json", identity)
    classifications = _classification(float(pbo["pbo"]), float(dsr["dsr_probability"]), float(placebo["empirical_pvalue"]))
    summary = {
        "status": "COMPLETE",
        "baseline": protocol["active_baseline"],
        "reproduction": reproduction,
        "pbo": pbo,
        "deflated_sharpe": dsr,
        "placebo": placebo,
        "trial_ledger": ledger,
        "interpretation": classifications,
        "composite_pass_gate": False,
    }
    _write_json(ARTIFACTS / "metrics.json", summary)
    elapsed = time.perf_counter() - started
    _write_documents(
        execution_commit=execution_commit,
        reproduction=reproduction,
        pbo=pbo,
        dsr=dsr,
        placebo=placebo,
        ledger=ledger,
        surface=surface,
        neighbors=neighbors,
        elapsed=elapsed,
    )
    manifest = build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX01",
            "date": "2026-09-02",
            "status": "COMPLETE",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": protocol["active_baseline"],
            "protocol_sha256": _file_sha(PROTOCOL_PATH),
            "visible_sample_end": "2025-12-31",
            "locked_test_end": "2026-09-01",
            "locked_test_accessed": True,
        },
    )
    validate_experiment_archive(EXPERIMENT_DIR)
    return {**summary, "manifest_file_count": len(manifest["files"]), "elapsed_seconds": elapsed}


def _archive_error(exc: Exception) -> None:
    _write_json(
        ARTIFACTS / "error.json",
        {"status": "ERROR", "type": type(exc).__name__, "message": str(exc)},
    )
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        f"# 0902_EX01 执行过程\n\n执行在不可变检查处停止：`{type(exc).__name__}: {exc}`。\n",
        encoding="utf-8",
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text(
        "# 0902_EX01 结论\n\n状态：`ERROR`。未改变策略或协议；错误证据已保留。\n",
        encoding="utf-8",
    )
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX01",
            "date": "2026-09-02",
            "status": "ERROR",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": protocol["active_baseline"],
            "protocol_sha256": _file_sha(PROTOCOL_PATH),
            "visible_sample_end": "2025-12-31",
            "locked_test_end": "2026-09-01",
            "locked_test_accessed": False,
        },
    )


def main() -> int:
    try:
        result = run()
    except Exception as exc:
        _archive_error(exc)
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
