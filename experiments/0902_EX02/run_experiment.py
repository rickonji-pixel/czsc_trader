from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from czsc_trader.backtest import run_backtest
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import MarketData, load_market_data
from czsc_trader.execution_policy import (
    ExecutionSimulation,
    entry_limit_series,
    policy_metrics,
    select_execution_candidate,
    simulate_limit_policy,
)
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.factors import generate_factor_frame
from czsc_trader.strategy_metrics import strategy_comparison_metrics


EXPERIMENT_DIR = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT_DIR / "artifacts"
PROTOCOL_PATH = ARTIFACTS / "protocol.json"
FROZEN_PATH = ARTIFACTS / "frozen_execution_policy.json"
RAW_DIR = REPO_ROOT / "data" / "raw"
BASELINE_ROOT = REPO_ROOT / "configs" / "rule_baselines"
LOCKED_TEST_ACCESSED = False


def _json_default(value: object) -> object:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value))
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")


def _file_sha(path: Path) -> str:
    content = path.read_bytes()
    if path.suffix.lower() in {".csv", ".html", ".json", ".md", ".py", ".txt"}:
        content = content.replace(b"\r\n", b"\n")
    return sha256(content).hexdigest()


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _protocol() -> dict[str, object]:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    if payload.get("experiment_id") != "0902_EX02" or payload.get("status") != "PRE_REGISTERED":
        raise ValueError("execution-policy protocol identity differs")
    return payload


def assert_freeze_before_test(frozen_path: Path, test_accessed: bool) -> None:
    """Refuse any locked-test access before a non-empty frozen policy exists."""
    if bool(test_accessed):
        raise ValueError("locked test was already accessed before this guard")
    if not Path(frozen_path).is_file() or Path(frozen_path).stat().st_size <= 2:
        raise ValueError("frozen execution policy must exist before locked test access")


def _critical_hashes() -> dict[str, str]:
    paths = (
        BASELINE_ROOT / "registry.json",
        BASELINE_ROOT / "baseline_20260901.json",
        RAW_DIR / "588080_manifest.json",
        RAW_DIR / "588080_validation.json",
        REPO_ROOT / "experiments" / "0901_EX20" / "artifacts" / "frozen_challenger.json",
    )
    return {path.relative_to(REPO_ROOT).as_posix(): _file_sha(path) for path in paths}


def _indexed_daily(data: MarketData) -> pd.DataFrame:
    daily = data.daily.copy().set_index("dt").sort_index()
    daily.index = pd.DatetimeIndex(pd.to_datetime(daily.index), name="dt")
    return daily


def _baseline_path(
    cutoff: str,
    protocol: dict[str, object],
) -> tuple[MarketData, pd.DataFrame, pd.Series, dict[str, object]]:
    data = load_market_data(RAW_DIR, "588080.SH", "etf", cutoff=cutoff)
    baseline = resolve_baseline(BASELINE_ROOT, "baseline_20260901", symbol="588080.SH")
    expected = protocol["baseline"]
    if baseline.version != expected["version"] or baseline.sha256 != expected["sha256"]:
        raise ValueError("active baseline identity differs from protocol")
    factors = generate_factor_frame(data).frame
    daily = _indexed_daily(data)
    applied = apply_resolved_baseline(
        factors,
        baseline,
        daily_close=daily["close"].reindex(factors.index),
    )
    target = applied.target_position.astype(float)
    prices = daily.reindex(target.index)
    if prices[["open", "high", "low", "close"]].isna().any().any():
        raise ValueError("daily prices do not align to baseline target")
    return data, prices, target, {"version": baseline.version, "sha256": baseline.sha256}


def _year_quality(
    simulation: ExecutionSimulation,
) -> pd.DataFrame:
    returns = simulation.equity.astype(float).pct_change().fillna(0.0)
    cycles = simulation.cycles.copy()
    if not cycles.empty:
        cycles["signal_date"] = pd.to_datetime(cycles["signal_date"])
    rows: list[dict[str, object]] = []
    for year in sorted(set(returns.index.year)):
        cycle_count = int((cycles["signal_date"].dt.year == year).sum()) if not cycles.empty else 0
        if cycle_count == 0:
            continue
        selected = returns.loc[returns.index.year == year]
        curve = selected.add(1.0).cumprod()
        total_return = float(curve.iloc[-1] - 1.0)
        annualized = float(curve.iloc[-1] ** (252.0 / len(curve)) - 1.0)
        drawdown = float(curve.div(curve.cummax()).sub(1.0).min())
        calmar = annualized / abs(drawdown) if abs(drawdown) > 1e-12 else float("inf")
        rows.append(
            {
                "year": year,
                "cycle_count": cycle_count,
                "return": total_return,
                "max_drawdown": drawdown,
                "calmar": calmar,
            }
        )
    return pd.DataFrame(rows)


def _candidate_values(start: float, stop: float, step: float) -> list[float]:
    count = int(round((float(stop) - float(start)) / float(step))) + 1
    return [float(start) + position * float(step) for position in range(count)]


def _comparison_from_backtest(
    daily: pd.DataFrame,
    target: pd.Series,
    fee_rate: float,
    init_cash: float,
    *,
    initial_target: float = 0.0,
    initial_signal_date: pd.Timestamp | None = None,
) -> dict[str, float | None]:
    result = run_backtest(
        daily,
        target,
        fee_rate=fee_rate,
        init_cash=init_cash,
        initial_target=initial_target,
        initial_signal_date=initial_signal_date,
    )
    return strategy_comparison_metrics(
        result.equity,
        result.orders,
        init_cash,
        float(result.metrics["sharpe"]),
    )


def _candidate_record(
    candidate_id: str,
    family: str,
    parameter: float,
    simulation: ExecutionSimulation,
    protocol: dict[str, object],
) -> tuple[dict[str, object], pd.DataFrame]:
    metrics = policy_metrics(simulation, float(protocol["init_cash"]))
    yearly = _year_quality(simulation)
    worst_calmar = float(yearly["calmar"].min()) if not yearly.empty else float("-inf")
    worst_drawdown = float(yearly["max_drawdown"].min()) if not yearly.empty else float("-inf")
    record = {
        "candidate_id": candidate_id,
        "family": family,
        "family_rank": 0 if family == "fixed" else 1,
        "parameter": parameter,
        "worst_calmar": worst_calmar,
        "worst_drawdown": worst_drawdown,
        **metrics,
    }
    yearly.insert(0, "candidate_id", candidate_id)
    return record, yearly


def _warning_gap_q05(daily: pd.DataFrame, target: pd.Series) -> float:
    previous = target.shift(1, fill_value=0.0)
    signals = target.index[target.eq(1.0) & previous.eq(0.0)]
    locations = {date: position for position, date in enumerate(daily.index)}
    gaps: list[float] = []
    for signal_date in signals:
        position = locations.get(signal_date)
        if position is None or position + 1 >= len(daily):
            continue
        next_date = daily.index[position + 1]
        gaps.append(float(daily.loc[next_date, "open"] / daily.loc[signal_date, "close"] - 1.0))
    if not gaps:
        raise ValueError("research set has no entry gap observations")
    return float(np.quantile(np.asarray(gaps, dtype=float), 0.05))


def _slice_test_simulation(
    simulation: ExecutionSimulation,
    start: pd.Timestamp,
) -> ExecutionSimulation:
    state = simulation.daily_state.loc[simulation.daily_state.index >= start].copy()
    orders = simulation.orders.loc[
        pd.to_datetime(simulation.orders["execution_date"]) >= start
    ].reset_index(drop=True)
    cycles = simulation.cycles.copy()
    if not cycles.empty:
        cycles = cycles.loc[pd.to_datetime(cycles["first_execution_date"]) >= start].reset_index(drop=True)
    return ExecutionSimulation(state["equity"].rename("equity"), orders, state, cycles, {})


def _write_completion_documents(
    status: str,
    execution_commit: str,
    selected: dict[str, object],
    research: dict[str, object],
    test: dict[str, object],
    reference: dict[str, object],
    elapsed: float,
) -> None:
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0902_EX02 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`。",
                "- 研究集：2020—2025；候选共50套（固定比例29套、ATR21套）。",
                f"- 冻结候选：`{selected['candidate_id']}`，公式`{selected['family']}`，参数`{float(selected['parameter']):.6f}`。",
                f"- 研究集T+1成交率：`{float(research['t1_fill_rate']):.4f}`。",
                "- 冻结文件写入并哈希后加载2026测试集。",
                f"- 总耗时：{elapsed:.2f}秒。",
                "- 活动基线、行情元数据和既有实验身份保持不变。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    recommendation = "建议进入晋升确认" if status == "COMPLETE" else "保留研究证据，不建议晋升"
    (EXPERIMENT_DIR / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0902_EX02 结论",
                "",
                f"正式状态：`{status}`。{recommendation}。",
                "",
                f"冻结执行规则为`{selected['family']}`，参数`{float(selected['parameter']):.6f}`；研究集T+1成交率`{float(research['t1_fill_rate']):.2%}`、最终成交率`{float(research['final_fill_rate']):.2%}`。",
                f"2026测试集T+1成交率`{float(test['t1_fill_rate']):.2%}`、最终成交率`{float(test['final_fill_rate']):.2%}`。",
                f"2026冻结执行规则收益率`{float(test['return']):.2%}`、最大回撤`{float(test['max_drawdown']):.2%}`、夏普率`{float(test['sharpe']):.4f}`。",
                f"2026无条件次日开盘基准收益率`{float(reference['return']):.2%}`、最大回撤`{float(reference['max_drawdown']):.2%}`、夏普率`{float(reference['sharpe']):.4f}`。",
                "",
                "本实验验证限价执行的成交覆盖与价格保护关系。活动信号基线保持不变，活动执行规则尚未创建。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def run() -> dict[str, object]:
    global LOCKED_TEST_ACCESSED
    started = time.perf_counter()
    protocol = _protocol()
    before = _critical_hashes()
    execution_commit = _git_head()
    fee = float(protocol["fee_rate"])
    cash = float(protocol["init_cash"])

    research_data, research_daily, research_target, baseline_identity = _baseline_path(
        str(protocol["research_end"]), protocol
    )
    candidate_rows: list[dict[str, object]] = []
    yearly_frames: list[pd.DataFrame] = []
    simulations: dict[str, ExecutionSimulation] = {}
    for family, values in (
        (
            "fixed",
            _candidate_values(
                float(protocol["fixed_premium_start"]),
                float(protocol["fixed_premium_stop"]),
                float(protocol["fixed_premium_step"]),
            ),
        ),
        (
            "atr",
            _candidate_values(
                float(protocol["atr_multiplier_start"]),
                float(protocol["atr_multiplier_stop"]),
                float(protocol["atr_multiplier_step"]),
            ),
        ),
    ):
        for position, parameter in enumerate(values):
            candidate_id = f"{family}_{position:02d}"
            limits = entry_limit_series(
                research_daily,
                family,
                parameter,
                atr_window=int(protocol["atr_window"]),
                tick=float(protocol["tick"]),
            )
            simulation = simulate_limit_policy(
                research_daily,
                research_data.intraday,
                research_target,
                limits,
                fee_rate=fee,
                init_cash=cash,
                diagnostic_quantity=int(protocol["diagnostic_quantity"]),
            )
            record, yearly = _candidate_record(
                candidate_id, family, parameter, simulation, protocol
            )
            candidate_rows.append(record)
            yearly_frames.append(yearly)
            simulations[candidate_id] = simulation

    candidates = pd.DataFrame(candidate_rows)
    yearly_metrics = pd.concat(yearly_frames, ignore_index=True)
    selected_row = select_execution_candidate(
        candidates, float(protocol["minimum_t1_fill_rate"])
    )
    selected = selected_row.to_dict()
    selected_simulation = simulations[str(selected["candidate_id"])]
    research_metrics = policy_metrics(selected_simulation, cash)
    warning_gap = _warning_gap_q05(research_daily, research_target)

    _write_csv(ARTIFACTS / "candidate_metrics.csv", candidates)
    _write_csv(ARTIFACTS / "candidate_year_metrics.csv", yearly_metrics)
    _write_csv(ARTIFACTS / "selected_research_orders.csv", selected_simulation.orders)
    _write_csv(ARTIFACTS / "selected_research_cycles.csv", selected_simulation.cycles)
    _write_csv(ARTIFACTS / "selected_research_daily.csv", selected_simulation.daily_state.reset_index())
    research_reference = _comparison_from_backtest(
        research_daily, research_target, fee, cash
    )
    frozen = {
        "schema_version": 1,
        "status": "FROZEN",
        "experiment_id": "0902_EX02",
        "symbol": "588080.SH",
        "baseline": baseline_identity,
        "family": selected["family"],
        "parameter": float(selected["parameter"]),
        "atr_window": int(protocol["atr_window"]),
        "tick": float(protocol["tick"]),
        "fee_rate": fee,
        "minimum_t1_fill_rate": float(protocol["minimum_t1_fill_rate"]),
        "research_t1_fill_rate": float(research_metrics["t1_fill_rate"]),
        "warning_gap_q05": warning_gap,
        "entry_order_type": "limit",
        "exit_primary_order_type": "limit_at_legal_lower_bound",
        "exit_continuous_fallback": "five_level_immediate_to_limit_with_protection",
        "research_data_hashes": research_data.hashes,
        "execution_commit": execution_commit,
    }
    _write_json(FROZEN_PATH, frozen)
    frozen_sha = _file_sha(FROZEN_PATH)
    assert_freeze_before_test(FROZEN_PATH, LOCKED_TEST_ACCESSED)

    LOCKED_TEST_ACCESSED = True
    test_data, full_daily, full_target, test_baseline_identity = _baseline_path(
        str(protocol["test_end"]), protocol
    )
    if test_baseline_identity != baseline_identity:
        raise ValueError("test baseline identity differs from research baseline")
    test_start = pd.Timestamp(str(protocol["test_start"]))
    prior_dates = full_daily.index[full_daily.index < test_start]
    if prior_dates.empty:
        raise ValueError("locked test has no prior signal session")
    prior_date = prior_dates[-1]
    simulation_daily = full_daily.loc[prior_date : pd.Timestamp(str(protocol["test_end"]))]
    simulation_target = full_target.reindex(simulation_daily.index)
    full_limits = entry_limit_series(
        full_daily,
        str(frozen["family"]),
        float(frozen["parameter"]),
        atr_window=int(frozen["atr_window"]),
        tick=float(frozen["tick"]),
    )
    test_simulation_full = simulate_limit_policy(
        simulation_daily,
        test_data.intraday,
        simulation_target,
        full_limits.reindex(simulation_daily.index),
        fee_rate=fee,
        init_cash=cash,
        diagnostic_quantity=int(protocol["diagnostic_quantity"]),
    )
    test_simulation = _slice_test_simulation(test_simulation_full, test_start)
    test_metrics = policy_metrics(test_simulation, cash)
    test_daily = full_daily.loc[test_start : pd.Timestamp(str(protocol["test_end"]))]
    test_target = full_target.reindex(test_daily.index)
    test_reference = _comparison_from_backtest(
        test_daily,
        test_target,
        fee,
        cash,
        initial_target=float(full_target.loc[prior_date]),
        initial_signal_date=prior_date,
    )
    _write_json(
        ARTIFACTS / "test_metrics.json",
        {
            "frozen_policy_sha256": frozen_sha,
            "selected_execution": test_metrics,
            "unconditional_next_open": test_reference,
        },
    )
    _write_csv(ARTIFACTS / "test_orders.csv", test_simulation.orders)
    _write_csv(ARTIFACTS / "test_cycles.csv", test_simulation.cycles)
    _write_csv(ARTIFACTS / "test_daily.csv", test_simulation.daily_state.reset_index())

    after = _critical_hashes()
    identity = {
        "status": "PASS" if before == after else "FAIL",
        "critical_hashes_before": before,
        "critical_hashes_after": after,
        "critical_hashes_unchanged": before == after,
        "baseline": baseline_identity,
        "execution_commit": execution_commit,
        "frozen_policy_sha256": frozen_sha,
        "frozen_before_test": True,
        "locked_test_accessed": LOCKED_TEST_ACCESSED,
        "research_data_hashes": research_data.hashes,
        "test_data_hashes": test_data.hashes,
    }
    if before != after:
        raise ValueError("critical identities changed during execution")
    _write_json(ARTIFACTS / "identity_audit.json", identity)
    status = (
        "COMPLETE"
        if float(research_metrics["t1_fill_rate"]) >= float(protocol["minimum_t1_fill_rate"])
        else "FAIL"
    )
    summary = {
        "status": status,
        "baseline": baseline_identity,
        "selected_candidate": selected,
        "research_metrics": research_metrics,
        "research_unconditional_next_open": research_reference,
        "test_metrics": test_metrics,
        "test_unconditional_next_open": test_reference,
        "frozen_policy_sha256": frozen_sha,
        "locked_test_accessed": LOCKED_TEST_ACCESSED,
        "active_execution_policy_promoted": False,
    }
    _write_json(ARTIFACTS / "metrics.json", summary)
    elapsed = time.perf_counter() - started
    _write_completion_documents(
        status,
        execution_commit,
        selected,
        research_metrics,
        test_metrics,
        test_reference,
        elapsed,
    )
    manifest = build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX02",
            "date": "2026-09-02",
            "status": status,
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": baseline_identity,
            "protocol_sha256": _file_sha(PROTOCOL_PATH),
            "visible_sample_end": str(protocol["research_end"]),
            "locked_test_end": str(protocol["test_end"]),
            "locked_test_accessed": LOCKED_TEST_ACCESSED,
            "frozen_execution_policy_sha256": frozen_sha,
        },
    )
    validate_experiment_archive(EXPERIMENT_DIR)
    return {**summary, "manifest_file_count": len(manifest["files"]), "elapsed_seconds": elapsed}


def _archive_error(exc: Exception) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _write_json(
        ARTIFACTS / "error.json",
        {"status": "ERROR", "type": type(exc).__name__, "message": str(exc)},
    )
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        f"# 0902_EX02 执行过程\n\n执行停止：`{type(exc).__name__}: {exc}`。\n",
        encoding="utf-8",
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text(
        "# 0902_EX02 结论\n\n正式状态：`ERROR`。活动基线和执行注册表保持不变。\n",
        encoding="utf-8",
    )
    protocol = _protocol()
    build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX02",
            "date": "2026-09-02",
            "status": "ERROR",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": protocol["baseline"],
            "protocol_sha256": _file_sha(PROTOCOL_PATH),
            "visible_sample_end": str(protocol["research_end"]),
            "locked_test_end": str(protocol["test_end"]),
            "locked_test_accessed": LOCKED_TEST_ACCESSED,
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
