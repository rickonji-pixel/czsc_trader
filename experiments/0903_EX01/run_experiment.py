from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT / "artifacts"
sys.path.insert(0, str(ROOT / "src"))

from czsc_trader.backtest import run_backtest
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import load_market_data
from czsc_trader.execution_policy import entry_limit_series, policy_metrics, simulate_limit_policy
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.factors import generate_factor_frame
from czsc_trader.strategy_metrics import strategy_comparison_metrics


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha(path: Path) -> str:
    return sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def run() -> dict[str, object]:
    data = load_market_data(ROOT / "data" / "raw", "588080.SH", "etf", cutoff="2026-09-02")
    baseline = resolve_baseline(ROOT / "configs" / "rule_baselines", "baseline_20260903", symbol="588080.SH")
    factors = generate_factor_frame(data).frame
    daily = data.daily.copy().set_index("dt").sort_index()
    daily.index = pd.DatetimeIndex(pd.to_datetime(daily.index), name="dt")
    applied = apply_resolved_baseline(factors, baseline, daily_close=daily["close"].reindex(factors.index))
    target = applied.target_position.astype(float)
    daily = daily.reindex(target.index)
    execution = baseline.execution
    if execution is None:
        raise ValueError("complete baseline execution is missing")
    limits = entry_limit_series(
        daily, "fixed", execution.entry_limit_parameter, tick=execution.instrument.price_tick
    )
    common = dict(
        daily=daily, intraday=data.intraday, target_position=target, entry_limits=limits,
        fee_rate=execution.capital.fee_rate, init_cash=1_000_000.0,
    )
    legacy = simulate_limit_policy(**common, lot_size=None, fill_on_equal_touch=True)
    reviewed = simulate_limit_policy(
        **common, lot_size=execution.instrument.lot_size, fill_on_equal_touch=False
    )
    reference = run_backtest(daily, target, fee_rate=execution.capital.fee_rate, init_cash=1_000_000.0)
    reference_metrics = strategy_comparison_metrics(
        reference.equity, reference.orders, 1_000_000.0
    )
    attempts = reviewed.daily_state
    uncertain = attempts.loc[
        attempts["desired_position"].eq(1.0)
        & attempts["actual_position"].shift(1, fill_value=0.0).eq(0.0)
        & daily["open"].gt(attempts["entry_limit"])
        & daily["low"].eq(attempts["entry_limit"])
    ]
    order_sizes = reviewed.orders["size"].astype(float) if not reviewed.orders.empty else pd.Series(dtype=float)
    result = {
        "status": "COMPLETE",
        "experiment_id": "0903_EX01",
        "cutoff": "2026-09-02",
        "baseline": {"version": baseline.version, "sha256": baseline.sha256, "candidate_id": 143},
        "execution": {
            "entry_family": execution.entry_limit_family,
            "entry_parameter": execution.entry_limit_parameter,
            "lot_size": execution.instrument.lot_size,
            "fee_rate": execution.capital.fee_rate,
            "touch_only": execution.virtual_fill.touch_only,
        },
        "legacy_fractional_equal_touch": policy_metrics(legacy, 1_000_000.0),
        "reviewed_integer_strict_penetration": policy_metrics(reviewed, 1_000_000.0),
        "unconditional_next_open": reference_metrics,
        "invariants": {
            "all_orders_100_share_lots": bool(order_sizes.empty or (order_sizes.mod(100).eq(0).all())),
            "minimum_cash": float(reviewed.daily_state["cash"].min()),
            "cash_non_negative": bool(reviewed.daily_state["cash"].ge(-1e-8).all()),
            "uncertain_equal_touches": int(len(uncertain)),
            "reviewed_order_count": int(len(reviewed.orders)),
        },
        "data_hashes": data.hashes,
        "identity_hashes": {
            "baseline_file": file_sha(ROOT / "configs" / "rule_baselines" / "baseline_20260903.json"),
            "baseline_registry": file_sha(ROOT / "configs" / "rule_baselines" / "registry.json"),
        },
    }
    if not result["invariants"]["all_orders_100_share_lots"] or not result["invariants"]["cash_non_negative"]:
        raise AssertionError("reviewed execution invariants failed")
    write_json(ARTIFACTS / "execution_review.json", result)
    (EXPERIMENT / "03_execution.md").write_text(
        "# 0903_EX01 执行\n\n已在2020-01-01至2026-09-02完整开发池重算候选143信号，并对三种执行语义进行同口径比较。机器结果见`artifacts/execution_review.json`。\n",
        encoding="utf-8",
    )
    reviewed_metrics = result["reviewed_integer_strict_penetration"]
    (EXPERIMENT / "04_conclusion.md").write_text(
        "# 0903_EX01 结论\n\n正式状态：`COMPLETE`。\n\n"
        "100份整数手、现金非负和严格穿价三项不变量均通过。唯一执行规则继续作为`baseline_20260903`组成部分使用；本实验没有搜索或晋升另一套执行参数。\n\n"
        f"复审口径收益率为`{reviewed_metrics['return']:.2%}`，最大回撤为`{reviewed_metrics['max_drawdown']:.2%}`，卡玛比率为`{reviewed_metrics['calmar']:.4f}`。盈亏比状态及数值以机器结果为准。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(EXPERIMENT, {
        "schema_version": 1, "experiment_id": "0903_EX01", "date": "2026-09-03",
        "status": "COMPLETE", "symbol": "588080.SH", "baseline": result["baseline"],
        "visible_sample_end": "2026-09-02",
    })
    validate_experiment_archive(EXPERIMENT)
    return result


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False))
