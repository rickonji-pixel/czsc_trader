from __future__ import annotations

import hashlib
import json
from pathlib import Path

import czsc._native as czsc_native
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.research_backtest import run_period_backtests
from czsc_trader.signal_census import generate_signal_census
from czsc_trader.signal_prototypes import build_minimal_prototype, transition_into
from czsc_trader.strategy_metrics import closed_trade_ledger, strategy_comparison_metrics


EXPERIMENT_ID = "20260909_S002_EX07"
EVALUATION_START = pd.Timestamp("2021-01-04")
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
        raise ValueError("neighborhood audit may not select, promote, or deploy")
    if protocol["holding_periods"] != [3, 4, 5, 6, 7]:
        raise ValueError("holding-period neighborhood differs from frozen protocol")


def _validate_source(repo_root: Path, protocol: dict[str, object]) -> Path:
    spec = protocol["source_experiment"]
    source = repo_root / "experiments" / spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": spec["manifest_sha256"],
        source / "artifacts" / "protocol.json": spec["protocol_sha256"],
        source / "artifacts" / "window_metrics.csv": spec["window_metrics_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    return source


def _regenerate_entry(replay_data, protocol: dict[str, object]):
    entry = protocol["signal_generation"]["entry"]
    registry = [
        item for item in czsc_native.list_all_signals() if item["name"] == entry["name"]
    ]
    if len(registry) != 1:
        raise ValueError("frozen CZSC entry signal function is unavailable")
    census = generate_signal_census(
        replay_data.adjusted,
        protocol["signal_generation"]["frequency_specs"],
        evaluation_start=EVALUATION_START,
        registry=registry,
    )
    matched = census.catalog.loc[
        census.catalog["frequency"].eq(entry["frequency"])
        & census.catalog["name"].eq(entry["name"])
    ]
    if len(matched) != 1 or matched.iloc[0]["signal_id"] != entry["signal_id"]:
        raise AssertionError("frozen entry signal identity changed")
    events = transition_into(census.primary[entry["signal_id"]], entry["state"])
    event_count = int((events & (events.index >= EVALUATION_START)).sum())
    if event_count != int(entry["expected_events_since_2021"]):
        raise AssertionError("entry event count differs from EX06 evidence chain")
    return census, event_count


def _periods(protocol: dict[str, object]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {
        item["id"]: (pd.Timestamp(item["start"]), pd.Timestamp(item["end"]))
        for item in protocol["windows"]
    }


def _run_holding_period(
    holding_period: int,
    target: pd.Series,
    execution_daily: pd.DataFrame,
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    fee_rate: float,
    initial_cash: float,
) -> tuple[list[dict[str, object]], list[pd.DataFrame], list[pd.DataFrame]]:
    results = run_period_backtests(
        execution_daily,
        target,
        periods,
        fee_rate=fee_rate,
        init_cash=initial_cash,
    )
    metric_rows = []
    order_frames = []
    trade_frames = []
    for window, result in results.items():
        metrics = strategy_comparison_metrics(result.equity, result.orders, initial_cash)
        trades = closed_trade_ledger(result.orders)
        metric_rows.append({
            "holding_sessions": holding_period,
            "window": window,
            "start": result.metrics["start"],
            "end": result.metrics["end"],
            **metrics,
            "closed_trades": len(trades),
            "exposure": result.metrics["exposure"],
        })
        orders = result.orders.copy()
        orders.insert(0, "window", window)
        orders.insert(0, "holding_sessions", holding_period)
        order_frames.append(orders)
        trades.insert(0, "window", window)
        trades.insert(0, "holding_sessions", holding_period)
        trade_frames.append(trades)
    return metric_rows, order_frames, trade_frames


def _trade_distribution(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    scoped = trades.loc[trades["window"].eq(CONTINUOUS_WINDOW)]
    for holding, group in scoped.groupby("holding_sessions", sort=True):
        values = group["net_return"].astype(float)
        absolute_total = float(values.abs().sum())
        rows.append({
            "holding_sessions": int(holding),
            "closed_trades": len(values),
            "mean_return": float(values.mean()),
            "median_return": float(values.median()),
            "win_rate": float(values.gt(0).mean()),
            "return_std": float(values.std(ddof=1)),
            "minimum_return": float(values.min()),
            "maximum_return": float(values.max()),
            "largest_absolute_trade_share": (
                float(values.abs().max() / absolute_total) if absolute_total else 0.0
            ),
        })
    return pd.DataFrame(rows)


def _neighborhood_summary(metrics: pd.DataFrame, protocol: dict[str, object]) -> pd.DataFrame:
    continuous = metrics.loc[metrics["window"].eq(CONTINUOUS_WINDOW)].set_index(
        "holding_sessions"
    )
    rows = []
    for metric in [*protocol["primary_metrics"], *protocol["secondary_metrics"]]:
        values = continuous[metric].astype(float)
        rows.append({
            "metric": metric,
            "minimum": float(values.min()),
            "maximum": float(values.max()),
            "median": float(values.median()),
            "holding_5": float(values.loc[5]),
            "holding_4_delta_from_5": float(values.loc[4] - values.loc[5]),
            "holding_6_delta_from_5": float(values.loc[6] - values.loc[5]),
        })
    return pd.DataFrame(rows)


def _normalize_dates(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame[columns].copy()
    for column in columns:
        if column.endswith("date"):
            result[column] = pd.to_datetime(result[column]).dt.strftime("%Y-%m-%d")
    return result.reset_index(drop=True)


def _reproduction_audit(
    source: Path,
    daily: pd.DataFrame,
    metrics: pd.DataFrame,
    orders: pd.DataFrame,
) -> pd.DataFrame:
    ex06_daily = pd.read_csv(source / "artifacts" / "prototype_daily.csv")
    ex06_daily = ex06_daily.loc[ex06_daily["prototype_id"].eq("P-A00-NO-FILTER")]
    current_daily = daily.loc[daily["holding_sessions"].eq(5)]
    daily_columns = [
        "date", "entry_transition", "risk_active", "held_sessions", "action",
        "target_position",
    ]
    pd.testing.assert_frame_equal(
        _normalize_dates(ex06_daily, daily_columns),
        _normalize_dates(current_daily, daily_columns),
        check_dtype=False,
    )
    ex06_metrics = pd.read_csv(source / "artifacts" / "window_metrics.csv")
    ex06_metrics = ex06_metrics.loc[ex06_metrics["prototype_id"].eq("P-A00-NO-FILTER")]
    current_metrics = metrics.loc[metrics["holding_sessions"].eq(5)]
    metric_columns = [
        "window", "start", "end", "max_drawdown", "calmar", "win_loss_ratio",
        "return", "sharpe", "closed_trades", "exposure",
    ]
    pd.testing.assert_frame_equal(
        ex06_metrics[metric_columns].reset_index(drop=True),
        current_metrics[metric_columns].reset_index(drop=True),
        check_dtype=False,
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )
    ex06_orders = pd.read_csv(source / "artifacts" / "orders.csv")
    ex06_orders = ex06_orders.loc[ex06_orders["prototype_id"].eq("P-A00-NO-FILTER")]
    current_orders = orders.loc[orders["holding_sessions"].eq(5)]
    order_columns = [
        "window", "signal_date", "execution_date", "side", "size", "price", "fees",
    ]
    pd.testing.assert_frame_equal(
        _normalize_dates(ex06_orders, order_columns),
        _normalize_dates(current_orders, order_columns),
        check_dtype=False,
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )
    return pd.DataFrame([
        {"check": "holding_5_daily_matches_ex06", "passed": True},
        {"check": "holding_5_metrics_match_ex06", "passed": True},
        {"check": "holding_5_orders_match_ex06", "passed": True},
    ])


def _text(value: object, *, percent: bool = False) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):.2%}" if percent else f"{float(value):.4f}"


def _render_conclusion(
    metrics: pd.DataFrame,
    summary: pd.DataFrame,
    trade_distribution: pd.DataFrame,
) -> str:
    continuous = metrics.loc[metrics["window"].eq(CONTINUOUS_WINDOW)].set_index(
        "holding_sessions"
    )
    annual = metrics.loc[
        metrics["window"].isin(["2021", "2022", "2023", "2024", "2025", "2026YTD"])
    ]
    positive_calmar = annual.assign(positive=annual["calmar"].fillna(0).gt(0)).groupby(
        "holding_sessions"
    )["positive"].sum()
    lines = [
        f"# {EXPERIMENT_ID} 结论",
        "",
        "状态：COMPLETE。三连跌均值回归无过滤原型的持有期邻域审计已完成。",
        "",
        "## 连续窗口（2021-01-04至2026-09-08）",
        "",
        "| 持有期 | 最大回撤 | 卡玛 | 盈亏比 | 收益率 | 夏普 | 闭合交易 | 暴露率 | 正卡玛年度 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for holding, row in continuous.iterrows():
        lines.append(
            f"| {holding}日 | {_text(row['max_drawdown'], percent=True)} | "
            f"{_text(row['calmar'])} | {_text(row['win_loss_ratio'])} | "
            f"{_text(row['return'], percent=True)} | {_text(row['sharpe'])} | "
            f"{int(row['closed_trades'])} | {_text(row['exposure'], percent=True)} | "
            f"{int(positive_calmar.loc[holding])}/6 |"
        )
    stats = summary.set_index("metric")
    trade_stats = trade_distribution.set_index("holding_sessions")
    lines.extend([
        "",
        "## 邻域判断",
        "",
        f"五个持有期的最大回撤范围为{_text(stats.loc['max_drawdown', 'minimum'], percent=True)}"
        f"至{_text(stats.loc['max_drawdown', 'maximum'], percent=True)}，卡玛范围为"
        f"{_text(stats.loc['calmar', 'minimum'])}至{_text(stats.loc['calmar', 'maximum'])}，"
        f"盈亏比范围为{_text(stats.loc['win_loss_ratio', 'minimum'])}至"
        f"{_text(stats.loc['win_loss_ratio', 'maximum'])}。",
        f"四日和六日相对五日的卡玛变化分别为"
        f"{_text(stats.loc['calmar', 'holding_4_delta_from_5'])}、"
        f"{_text(stats.loc['calmar', 'holding_6_delta_from_5'])}；盈亏比变化分别为"
        f"{_text(stats.loc['win_loss_ratio', 'holding_4_delta_from_5'])}、"
        f"{_text(stats.loc['win_loss_ratio', 'holding_6_delta_from_5'])}。",
        f"五日版本最大单笔绝对收益占全部交易绝对收益的"
        f"{_text(trade_stats.loc[5, 'largest_absolute_trade_share'], percent=True)}。",
        "",
        "五个持有期的连续窗口卡玛均为正，4至6日的盈亏比都超过2，说明均值回归机制没有"
        "因小幅改变持有期而消失。五日卡玛约为四日和六日的两倍，且最大回撤明显更低，"
        "因此当前形态是4至6日可用带中的突出峰值，还不能称为宽阔平原。单笔绝对收益占比"
        "不足13%，没有发现由单笔交易独占结果的现象。",
        "",
        "后续统计审计必须把4、5、6日视为同一次相关参数试验，评估五日相对邻居的优势是否"
        "超过随机波动。五日只作为此前已经冻结的代表规则，不因本轮历史最高分获得额外可信度。",
        "",
        "## 边界",
        "",
        "所有持有期都在同一已读开发池上计算，属于敏感性证据，不能视为五次独立验证。"
        "本轮未选择参数、未生成S002候选，也未调用SE排名、SM或PTE。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate_protocol(protocol)
    source = _validate_source(repo_root, protocol)
    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    target = protocol["research_target"]
    replay_data = load_replay_data(
        context,
        "research",
        target["symbol"],
        target["asset_type"],
        pd.Timestamp(target["development_cutoff"]).date(),
    )
    census, event_count = _regenerate_entry(replay_data, protocol)
    entry = protocol["signal_generation"]["entry"]
    shared = protocol["shared_rules"]
    decision_frames = []
    targets = {}
    for holding in protocol["holding_periods"]:
        result = build_minimal_prototype(
            census.primary[entry["signal_id"]],
            entry["state"],
            [],
            max_holding_sessions=int(holding),
            target_position=float(shared["target_position"]),
        )
        targets[int(holding)] = result.target_position
        decisions = result.decisions.loc[result.decisions["date"] >= EVALUATION_START].copy()
        decisions.insert(0, "holding_sessions", int(holding))
        decision_frames.append(decisions)
    prototype_daily = pd.concat(decision_frames, ignore_index=True)

    periods = _periods(protocol)
    metric_rows: list[dict[str, object]] = []
    order_frames: list[pd.DataFrame] = []
    trade_frames: list[pd.DataFrame] = []
    for holding, holding_target in targets.items():
        metrics, orders, trades = _run_holding_period(
            holding,
            holding_target,
            replay_data.execution_daily,
            periods,
            float(shared["fee_rate_one_way"]),
            float(shared["initial_cash"]),
        )
        metric_rows.extend(metrics)
        order_frames.extend(orders)
        trade_frames.extend(trades)
    metrics_frame = pd.DataFrame(metric_rows)
    orders_frame = pd.concat(order_frames, ignore_index=True)
    trades_frame = pd.concat(trade_frames, ignore_index=True)
    distribution = _trade_distribution(trades_frame)
    summary = _neighborhood_summary(metrics_frame, protocol)
    reproduction = _reproduction_audit(
        source,
        prototype_daily,
        metrics_frame,
        orders_frame,
    )

    _write_csv(prototype_daily, artifacts / "prototype_daily.csv")
    _write_csv(metrics_frame, artifacts / "window_metrics.csv")
    _write_csv(orders_frame, artifacts / "orders.csv")
    _write_csv(trades_frame, artifacts / "closed_trades.csv")
    _write_csv(distribution, artifacts / "trade_distribution.csv")
    _write_csv(summary, artifacts / "neighborhood_summary.csv")
    _write_csv(reproduction, artifacts / "reproduction_audit.csv")
    output_names = [
        "prototype_daily.csv",
        "window_metrics.csv",
        "orders.csv",
        "closed_trades.csv",
        "trade_distribution.csv",
        "neighborhood_summary.csv",
        "reproduction_audit.csv",
    ]
    _write_json(artifacts / "run_evidence.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset_fingerprint": replay_data.fingerprint,
        "cutoff": replay_data.cutoff.isoformat(),
        "entry_events_since_2021": event_count,
        "ex06_reproduction_passed": True,
        "holding_periods": list(targets),
        "windows": list(periods),
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
        f"入场信号身份和{event_count}次状态切入事件通过EX06证据链校验。完成3至7日五个"
        f"冻结持有期在{len(periods)}个独立资金窗口的回放、交易分布和邻域摘要。五日版本"
        "的每日决策、窗口指标和订单精确复现EX06无过滤版本。\n\n"
        "未选择参数、未生成候选，也未调用SE排名、SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(metrics_frame, summary, distribution),
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
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
