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
from czsc_trader.moving_average import moving_average_signals
from czsc_trader.research_backtest import run_period_backtests
from czsc_trader.signal_census import generate_signal_census
from czsc_trader.signal_prototypes import build_minimal_prototype, transition_into
from czsc_trader.strategy_metrics import closed_trade_ledger, strategy_comparison_metrics


EXPERIMENT_ID = "20260909_S002_EX05"
EVALUATION_START = pd.Timestamp("2021-01-04")


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
        "candidate_generation",
        "parameter_search",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("prototype diagnosis may not select, promote, or deploy")


def _validate_sources(repo_root: Path, protocol: dict[str, object]) -> tuple[Path, Path]:
    specs = protocol["source_experiments"]
    hypothesis = repo_root / "experiments" / specs["hypothesis_review"]["experiment_id"]
    census = repo_root / "experiments" / specs["signal_census"]["experiment_id"]
    validate_experiment_archive(hypothesis)
    validate_experiment_archive(census)
    expected = {
        hypothesis / "experiment_manifest.json": specs["hypothesis_review"]["manifest_sha256"],
        hypothesis / "artifacts" / "hypotheses.json": specs["hypothesis_review"]["hypotheses_sha256"],
        census / "artifacts" / "signal_catalog.csv": specs["signal_census"]["signal_catalog_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    return hypothesis, census


def _regenerate_states(replay_data, protocol: dict[str, object]):
    names = {item["name"] for item in protocol["signal_generation"]["expected_signals"]}
    registry = [item for item in czsc_native.list_all_signals() if item["name"] in names]
    if {item["name"] for item in registry} != names:
        raise ValueError("one or more frozen CZSC signal functions are unavailable")
    return generate_signal_census(
        replay_data.adjusted,
        protocol["signal_generation"]["frequency_specs"],
        evaluation_start=EVALUATION_START,
        registry=registry,
    )


def _transition_audit(
    primary: pd.DataFrame,
    expected_signals: list[dict[str, object]],
    archived_events: pd.DataFrame,
) -> pd.DataFrame:
    state_id_by_key = {
        (row["signal_id"], row["state_primary"]): row["state_id"]
        for _, row in archived_events[
            ["signal_id", "state_primary", "state_id"]
        ].drop_duplicates().iterrows()
    }
    rows = []
    for item in expected_signals:
        signal_id = item["signal_id"]
        state = item["state"]
        state_id = state_id_by_key[(signal_id, state)]
        regenerated = transition_into(primary[signal_id], state)
        regenerated_dates = set(
            primary.index[regenerated & (primary.index >= EVALUATION_START)].strftime("%Y-%m-%d")
        )
        archived_dates = set(
            archived_events.loc[
                archived_events["state_id"].eq(state_id), "signal_date"
            ].astype(str)
        )
        rows.append({
            "signal_id": signal_id,
            "state_id": state_id,
            "frequency": item["frequency"],
            "name": item["name"],
            "state": state,
            "regenerated_events": len(regenerated_dates),
            "archived_events": len(archived_dates),
            "dates_match": regenerated_dates == archived_dates,
        })
    result = pd.DataFrame(rows)
    if not result["dates_match"].all():
        raise AssertionError("regenerated state transitions differ from EX02 evidence")
    return result


def _periods(protocol: dict[str, object]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {
        item["id"]: (pd.Timestamp(item["start"]), pd.Timestamp(item["end"]))
        for item in protocol["windows"]
    }


def _run_subject(
    subject: str,
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
    metrics_rows = []
    order_frames = []
    trade_frames = []
    for window, result in results.items():
        comparison = strategy_comparison_metrics(result.equity, result.orders, initial_cash)
        trades = closed_trade_ledger(result.orders)
        metrics_rows.append({
            "subject": subject,
            "window": window,
            "start": result.metrics["start"],
            "end": result.metrics["end"],
            **comparison,
            "closed_trades": len(trades),
            "exposure": result.metrics["exposure"],
        })
        orders = result.orders.copy()
        orders.insert(0, "window", window)
        orders.insert(0, "subject", subject)
        order_frames.append(orders)
        trades.insert(0, "window", window)
        trades.insert(0, "subject", subject)
        trade_frames.append(trades)
    return metrics_rows, order_frames, trade_frames


def _metric_text(value: object, *, percent: bool = False) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):.2%}" if percent else f"{float(value):.4f}"


def _render_conclusion(metrics: pd.DataFrame, decision_counts: pd.DataFrame) -> str:
    continuous = metrics.loc[metrics["window"] == "2021_2026YTD"].set_index("subject")
    prototypes = ("P-A-MEAN-REVERSION", "P-B-TREND-CONTINUATION")
    annual = metrics.loc[
        metrics["window"].isin(["2021", "2022", "2023", "2024", "2025", "2026YTD"])
        & metrics["subject"].isin(prototypes)
    ]
    positive_calmar = annual.assign(positive=annual["calmar"].fillna(0).gt(0)).groupby(
        "subject"
    )["positive"].sum()
    lines = [
        f"# {EXPERIMENT_ID} 结论",
        "",
        "状态：COMPLETE。两个预注册最小原型已完成同口径回放。",
        "",
        "## 连续窗口（2021-01-04至2026-09-08）",
        "",
        "| 对象 | 最大回撤 | 卡玛 | 盈亏比 | 收益率 | 夏普 | 闭合交易 | 暴露率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "P-A-MEAN-REVERSION": "原型A：三连跌均值回归",
        "P-B-TREND-CONTINUATION": "原型B：0轴上第3次金叉",
        "BUYHOLD": "BuyHold",
        "MA5_MA20": "MA5/MA20",
    }
    for subject in (*prototypes, "BUYHOLD", "MA5_MA20"):
        row = continuous.loc[subject]
        lines.append(
            f"| {labels[subject]} | {_metric_text(row['max_drawdown'], percent=True)} | "
            f"{_metric_text(row['calmar'])} | {_metric_text(row['win_loss_ratio'])} | "
            f"{_metric_text(row['return'], percent=True)} | {_metric_text(row['sharpe'])} | "
            f"{int(row['closed_trades'])} | {_metric_text(row['exposure'], percent=True)} |"
        )
    lines.extend(["", "## 机制诊断", ""])
    for subject in prototypes:
        actions = decision_counts.loc[decision_counts["prototype_id"] == subject].set_index(
            "action"
        )["count"]
        lines.append(
            f"- {labels[subject]}：6个年度窗口中{int(positive_calmar.get(subject, 0))}个卡玛为正；"
            f"连续窗口产生{int(continuous.loc[subject, 'closed_trades'])}笔闭合交易。"
            f"入场{int(actions.get('ENTER', 0))}次，风险退出{int(actions.get('EXIT_RISK', 0))}次，"
            f"持有期退出{int(actions.get('EXIT_TIME', 0))}次。"
        )
    primary = ("max_drawdown", "calmar", "win_loss_ratio")
    wins = {subject: 0 for subject in prototypes}
    ties = 0
    for metric in primary:
        left = continuous.loc[prototypes[0], metric]
        right = continuous.loc[prototypes[1], metric]
        if pd.isna(left) or pd.isna(right) or left == right:
            ties += 1
        elif left > right:
            wins[prototypes[0]] += 1
        else:
            wins[prototypes[1]] += 1
    leader = max(prototypes, key=lambda item: wins[item])
    lines.extend([
        "",
        f"连续窗口三个主指标中，原型A胜{wins[prototypes[0]]}项，原型B胜"
        f"{wins[prototypes[1]]}项，不可比或相同{ties}项。当前相对证据偏向"
        f"{labels[leader]}，该判断只用于确定下一轮研究顺序。",
        "",
        "## 边界",
        "",
        "五日持有期和两条风险过滤来自已读开发池，结果属于机制诊断，不能解释为样本外证明。"
        "本轮没有搜索参数、没有生成S002候选，也没有调用SM或PTE。分年度明细、订单和闭合"
        "交易见`artifacts/`。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate_protocol(protocol)
    hypothesis_dir, _ = _validate_sources(repo_root, protocol)

    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    target = protocol["research_target"]
    replay_data = load_replay_data(
        context,
        "research",
        target["symbol"],
        target["asset_type"],
        pd.Timestamp(target["development_cutoff"]).date(),
    )
    census = _regenerate_states(replay_data, protocol)
    expected_signals = protocol["signal_generation"]["expected_signals"]
    for item in expected_signals:
        matched = census.catalog.loc[
            census.catalog["frequency"].eq(item["frequency"])
            & census.catalog["name"].eq(item["name"])
        ]
        if len(matched) != 1 or matched.iloc[0]["signal_id"] != item["signal_id"]:
            raise AssertionError(f"frozen signal identity changed: {item['name']}")

    archived_events = pd.read_csv(hypothesis_dir / "artifacts" / "selected_events.csv")
    audit = _transition_audit(
        census.primary,
        expected_signals,
        archived_events,
    )

    shared = protocol["shared_rules"]
    risks = [
        (
            next(item["name"] for item in expected_signals if item["signal_id"] == risk["signal_id"]),
            census.primary[risk["signal_id"]],
            risk["state"],
        )
        for risk in shared["risk_states"]
    ]
    prototypes = {}
    decision_frames = []
    for spec in protocol["prototypes"]:
        result = build_minimal_prototype(
            census.primary[spec["entry_signal_id"]],
            spec["entry_state"],
            risks,
            max_holding_sessions=int(shared["max_holding_sessions"]),
            target_position=float(shared["target_position"]),
        )
        prototypes[spec["prototype_id"]] = result.target_position
        decisions = result.decisions.loc[result.decisions["date"] >= EVALUATION_START].copy()
        decisions.insert(0, "prototype_id", spec["prototype_id"])
        decision_frames.append(decisions)
    prototype_daily = pd.concat(decision_frames, ignore_index=True)
    decision_counts = (
        prototype_daily.groupby(["prototype_id", "action"], sort=True)
        .size()
        .rename("count")
        .reset_index()
    )

    adjusted_index = pd.DatetimeIndex(
        pd.to_datetime(replay_data.adjusted.daily["dt"]).dt.normalize(), name="dt"
    )
    benchmarks = {
        "BUYHOLD": pd.Series(1.0, index=adjusted_index, name="target_position"),
        "MA5_MA20": moving_average_signals(replay_data.adjusted.daily)[
            "target_position"
        ].reindex(adjusted_index).astype(float),
    }
    periods = _periods(protocol)
    metrics_rows: list[dict[str, object]] = []
    order_frames: list[pd.DataFrame] = []
    trade_frames: list[pd.DataFrame] = []
    for subject, subject_target in {**prototypes, **benchmarks}.items():
        metrics, orders, trades = _run_subject(
            subject,
            subject_target,
            replay_data.execution_daily,
            periods,
            float(shared["fee_rate_one_way"]),
            float(shared["initial_cash"]),
        )
        metrics_rows.extend(metrics)
        order_frames.extend(orders)
        trade_frames.extend(trades)
    metrics_frame = pd.DataFrame(metrics_rows)
    orders_frame = pd.concat(order_frames, ignore_index=True)
    trades_frame = pd.concat(trade_frames, ignore_index=True)

    generated_catalog = census.catalog.loc[
        census.catalog["signal_id"].isin([item["signal_id"] for item in expected_signals])
    ].copy()
    _write_csv(audit, artifacts / "signal_transition_audit.csv")
    _write_csv(generated_catalog, artifacts / "generated_signal_catalog.csv")
    _write_csv(prototype_daily, artifacts / "prototype_daily.csv")
    _write_csv(decision_counts, artifacts / "decision_counts.csv")
    _write_csv(metrics_frame, artifacts / "window_metrics.csv")
    _write_csv(orders_frame, artifacts / "orders.csv")
    _write_csv(trades_frame, artifacts / "closed_trades.csv")
    output_names = [
        "signal_transition_audit.csv",
        "generated_signal_catalog.csv",
        "prototype_daily.csv",
        "decision_counts.csv",
        "window_metrics.csv",
        "orders.csv",
        "closed_trades.csv",
    ]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset_fingerprint": replay_data.fingerprint,
        "cutoff": replay_data.cutoff.isoformat(),
        "transition_audit_passed": bool(audit["dates_match"].all()),
        "subjects": sorted([*prototypes, *benchmarks]),
        "windows": list(periods),
        "outputs": {
            name: {
                "bytes": (artifacts / name).stat().st_size,
                "sha256": _sha256(artifacts / name),
            }
            for name in output_names
        },
    }
    _write_json(artifacts / "run_evidence.json", evidence)
    (experiment_dir / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        "四条冻结信号已按原CZSC配置重新生成，其目标状态切入日期与EX02事件档案完全一致。"
        f"完成{len(prototypes)}个原型与{len(benchmarks)}个参照在{len(periods)}个独立资金窗口"
        "的回放，保存每日决策、订单、闭合交易和统一指标。\n\n"
        "未搜索参数、未生成候选，也未调用SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(metrics_frame, decision_counts),
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
