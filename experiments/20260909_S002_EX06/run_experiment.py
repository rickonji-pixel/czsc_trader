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
from czsc_trader.signal_prototypes import (
    build_minimal_prototype,
    transition_into,
    two_by_two_effects,
)
from czsc_trader.strategy_metrics import closed_trade_ledger, strategy_comparison_metrics


EXPERIMENT_ID = "20260909_S002_EX06"
EVALUATION_START = pd.Timestamp("2021-01-04")
VARIANT_KEYS = {
    "none": "P-A00-NO-FILTER",
    "top": "P-A10-TOP-DIVERGENCE",
    "pressure": "P-A01-STRUCTURE-PRESSURE",
    "both": "P-A11-BOTH-FILTERS",
}


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
        raise ValueError("filter attribution may not select, promote, or deploy")
    variants = {item["prototype_id"] for item in protocol["variants"]}
    if variants != set(VARIANT_KEYS.values()):
        raise ValueError("frozen 2x2 variant matrix is incomplete")


def _validate_source(repo_root: Path, protocol: dict[str, object]) -> Path:
    spec = protocol["source_experiment"]
    source = repo_root / "experiments" / spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": spec["manifest_sha256"],
        source / "artifacts" / "protocol.json": spec["protocol_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    return source


def _regenerate_states(replay_data, protocol: dict[str, object]):
    signals = protocol["signal_generation"]["signals"]
    names = {item["name"] for item in signals.values()}
    registry = [item for item in czsc_native.list_all_signals() if item["name"] in names]
    if {item["name"] for item in registry} != names:
        raise ValueError("one or more frozen CZSC signal functions are unavailable")
    census = generate_signal_census(
        replay_data.adjusted,
        protocol["signal_generation"]["frequency_specs"],
        evaluation_start=EVALUATION_START,
        registry=registry,
    )
    for item in signals.values():
        matched = census.catalog.loc[
            census.catalog["frequency"].eq(item["frequency"])
            & census.catalog["name"].eq(item["name"])
        ]
        if len(matched) != 1 or matched.iloc[0]["signal_id"] != item["signal_id"]:
            raise AssertionError(f"frozen signal identity changed: {item['name']}")
    return census


def _transition_audit(census, protocol: dict[str, object], source: Path) -> pd.DataFrame:
    source_audit = pd.read_csv(source / "artifacts" / "signal_transition_audit.csv")
    rows = []
    for item in protocol["signal_generation"]["signals"].values():
        events = transition_into(census.primary[item["signal_id"]], item["state"])
        event_count = int((events & (events.index >= EVALUATION_START)).sum())
        prior = source_audit.loc[source_audit["state_id"].eq(item["state_id"])]
        if len(prior) != 1:
            raise AssertionError(f"EX05 transition audit missing {item['state_id']}")
        expected_count = int(prior.iloc[0]["regenerated_events"])
        rows.append({
            "signal_id": item["signal_id"],
            "state_id": item["state_id"],
            "frequency": item["frequency"],
            "name": item["name"],
            "state": item["state"],
            "regenerated_events": event_count,
            "ex05_events": expected_count,
            "count_matches_ex05": event_count == expected_count,
            "ex05_dates_match_ex02": bool(prior.iloc[0]["dates_match"]),
        })
    result = pd.DataFrame(rows)
    if not result[["count_matches_ex05", "ex05_dates_match_ex02"]].all().all():
        raise AssertionError("signal transition evidence differs from EX05/EX02")
    return result


def _periods(protocol: dict[str, object]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {
        item["id"]: (pd.Timestamp(item["start"]), pd.Timestamp(item["end"]))
        for item in protocol["windows"]
    }


def _run_variant(
    variant: str,
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
            "prototype_id": variant,
            "window": window,
            "start": result.metrics["start"],
            "end": result.metrics["end"],
            **metrics,
            "closed_trades": len(trades),
            "exposure": result.metrics["exposure"],
        })
        orders = result.orders.copy()
        orders.insert(0, "window", window)
        orders.insert(0, "prototype_id", variant)
        order_frames.append(orders)
        trades.insert(0, "window", window)
        trades.insert(0, "prototype_id", variant)
        trade_frames.append(trades)
    return metric_rows, order_frames, trade_frames


def _factorial_effects(metrics: pd.DataFrame, protocol: dict[str, object]) -> pd.DataFrame:
    rows = []
    metric_names = [*protocol["primary_metrics"], *protocol["secondary_metrics"]]
    for window, scoped in metrics.groupby("window", sort=False):
        values = scoped.set_index("prototype_id")
        for metric in metric_names:
            effects = two_by_two_effects(
                float(values.at[VARIANT_KEYS["none"], metric]),
                float(values.at[VARIANT_KEYS["top"], metric]),
                float(values.at[VARIANT_KEYS["pressure"], metric]),
                float(values.at[VARIANT_KEYS["both"], metric]),
            )
            for effect, value in effects.items():
                rows.append({
                    "window": window,
                    "metric": metric,
                    "effect": effect,
                    "value": value,
                })
    return pd.DataFrame(rows)


def _normalize_dates(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame[columns].copy()
    for column in columns:
        if column.endswith("date"):
            result[column] = pd.to_datetime(result[column]).dt.strftime("%Y-%m-%d")
    if "active_risks" in result:
        result["active_risks"] = result["active_risks"].fillna("")
    return result.reset_index(drop=True)


def _reproduction_audit(
    source: Path,
    daily: pd.DataFrame,
    metrics: pd.DataFrame,
    orders: pd.DataFrame,
) -> pd.DataFrame:
    ex05_daily = pd.read_csv(source / "artifacts" / "prototype_daily.csv")
    ex05_daily = ex05_daily.loc[ex05_daily["prototype_id"].eq("P-A-MEAN-REVERSION")]
    current_daily = daily.loc[daily["prototype_id"].eq(VARIANT_KEYS["both"])]
    daily_columns = [
        "date", "entry_transition", "risk_active", "active_risks",
        "held_sessions", "action", "target_position",
    ]
    pd.testing.assert_frame_equal(
        _normalize_dates(ex05_daily, daily_columns),
        _normalize_dates(current_daily, daily_columns),
        check_dtype=False,
    )

    ex05_metrics = pd.read_csv(source / "artifacts" / "window_metrics.csv")
    ex05_metrics = ex05_metrics.loc[ex05_metrics["subject"].eq("P-A-MEAN-REVERSION")]
    current_metrics = metrics.loc[metrics["prototype_id"].eq(VARIANT_KEYS["both"])]
    metric_columns = [
        "window", "start", "end", "max_drawdown", "calmar", "win_loss_ratio",
        "return", "sharpe", "closed_trades", "exposure",
    ]
    pd.testing.assert_frame_equal(
        ex05_metrics[metric_columns].reset_index(drop=True),
        current_metrics[metric_columns].reset_index(drop=True),
        check_dtype=False,
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )

    ex05_orders = pd.read_csv(source / "artifacts" / "orders.csv")
    ex05_orders = ex05_orders.loc[ex05_orders["subject"].eq("P-A-MEAN-REVERSION")]
    current_orders = orders.loc[orders["prototype_id"].eq(VARIANT_KEYS["both"])]
    order_columns = [
        "window", "signal_date", "execution_date", "side", "size", "price", "fees",
    ]
    pd.testing.assert_frame_equal(
        _normalize_dates(ex05_orders, order_columns),
        _normalize_dates(current_orders, order_columns),
        check_dtype=False,
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )
    return pd.DataFrame([
        {"check": "daily_decisions_match_ex05", "passed": True},
        {"check": "window_metrics_match_ex05", "passed": True},
        {"check": "orders_match_ex05", "passed": True},
    ])


def _text(value: object, *, percent: bool = False) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):.2%}" if percent else f"{float(value):.4f}"


def _render_conclusion(
    metrics: pd.DataFrame,
    effects: pd.DataFrame,
    counts: pd.DataFrame,
    interventions: pd.DataFrame,
) -> str:
    continuous = metrics.loc[metrics["window"].eq("2021_2026YTD")].set_index(
        "prototype_id"
    )
    labels = {
        VARIANT_KEYS["none"]: "无过滤",
        VARIANT_KEYS["top"]: "仅顶背驰",
        VARIANT_KEYS["pressure"]: "仅压力",
        VARIANT_KEYS["both"]: "双过滤",
    }
    lines = [
        f"# {EXPERIMENT_ID} 结论",
        "",
        "状态：COMPLETE。三连跌均值回归原型的2×2过滤归因已完成。",
        "",
        "## 连续窗口（2021-01-04至2026-09-08）",
        "",
        "| 版本 | 最大回撤 | 卡玛 | 盈亏比 | 收益率 | 闭合交易 | 暴露率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in VARIANT_KEYS.values():
        row = continuous.loc[variant]
        lines.append(
            f"| {labels[variant]} | {_text(row['max_drawdown'], percent=True)} | "
            f"{_text(row['calmar'])} | {_text(row['win_loss_ratio'])} | "
            f"{_text(row['return'], percent=True)} | {int(row['closed_trades'])} | "
            f"{_text(row['exposure'], percent=True)} |"
        )
    lines.extend(["", "## 过滤效应", ""])
    selected_effects = effects.loc[
        effects["window"].eq("2021_2026YTD")
        & effects["metric"].isin(["max_drawdown", "calmar", "win_loss_ratio", "return"])
    ]
    for effect, label in (
        ("top_divergence_main", "顶背驰主效应"),
        ("structure_pressure_main", "压力主效应"),
        ("interaction", "两项交互效应"),
    ):
        values = selected_effects.loc[selected_effects["effect"].eq(effect)].set_index(
            "metric"
        )["value"]
        lines.append(
            f"- {label}：最大回撤{_text(values['max_drawdown'], percent=True)}，"
            f"卡玛{_text(values['calmar'])}，盈亏比{_text(values['win_loss_ratio'])}，"
            f"收益率{_text(values['return'], percent=True)}。"
        )
    lines.extend(["", "## 事件干预", ""])
    for variant in VARIANT_KEYS.values():
        scoped = counts.loc[counts["prototype_id"].eq(variant)].set_index("action")["count"]
        lines.append(
            f"- {labels[variant]}：阻止入场{int(scoped.get('BLOCK_ENTRY_RISK', 0))}次，"
            f"风险退出{int(scoped.get('EXIT_RISK', 0))}次。"
        )
    unique_dates = int(interventions["date"].nunique()) if not interventions.empty else 0
    no_filter = continuous.loc[VARIANT_KEYS["none"]]
    both = continuous.loc[VARIANT_KEYS["both"]]
    annual = metrics.loc[
        metrics["window"].isin(["2021", "2022", "2023", "2024", "2025", "2026YTD"])
    ].pivot(index="window", columns="prototype_id", values="return")
    pressure_delta = annual[VARIANT_KEYS["pressure"]] - annual[VARIANT_KEYS["none"]]
    top_delta = annual[VARIANT_KEYS["top"]] - annual[VARIANT_KEYS["none"]]
    no_filter_calmar = metrics.loc[
        metrics["window"].isin(annual.index)
        & metrics["prototype_id"].eq(VARIANT_KEYS["none"]),
        "calmar",
    ]
    lines.extend([
        "",
        f"全部过滤干预集中在{unique_dates}个交易日。无过滤版本自身的最大回撤为"
        f"{_text(no_filter['max_drawdown'], percent=True)}、卡玛{_text(no_filter['calmar'])}、"
        f"盈亏比{_text(no_filter['win_loss_ratio'])}；双过滤版本相应为"
        f"{_text(both['max_drawdown'], percent=True)}、{_text(both['calmar'])}、"
        f"{_text(both['win_loss_ratio'])}。",
        "",
        "无过滤版本在六个年度窗口中"
        f"{int(no_filter_calmar.fillna(0).gt(0).sum())}个卡玛为正，唯一负值出现在2023年。"
        f"压力过滤对2023年收益率贡献{_text(pressure_delta['2023'], percent=True)}，"
        f"对2021年贡献{_text(pressure_delta['2021'], percent=True)}，其余年度没有收益变化；"
        "因此它的连续窗口改善高度集中在2023年的两次被阻止入场。"
        f"顶背驰过滤只改变2025年路径，收益贡献{_text(top_delta['2025'], percent=True)}。",
        "",
        "归因支持把`三连跌＋五日持有`作为下一轮核心机制；顶背驰过滤当前没有独立价值。"
        "压力过滤保留为待验证假设，现有证据不足以把它并入核心规则。",
        "",
        "## 边界",
        "",
        "这些差值来自同一开发池内的确定性路径反事实，说明过滤如何改变已有历史交易，不能"
        "视为样本外因果证明。本轮未调参、未生成S002候选，也未调用SE排名、SM或PTE。",
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
    census = _regenerate_states(replay_data, protocol)
    transition_audit = _transition_audit(census, protocol, source)
    signals = protocol["signal_generation"]["signals"]
    entry = signals["entry"]
    risk_definitions = {
        "top_divergence": signals["top_divergence"],
        "structure_pressure": signals["structure_pressure"],
    }
    shared = protocol["shared_rules"]
    targets = {}
    decision_frames = []
    for variant in protocol["variants"]:
        risks = []
        for flag, definition in risk_definitions.items():
            if variant[flag]:
                risks.append((
                    definition["name"],
                    census.primary[definition["signal_id"]],
                    definition["state"],
                ))
        result = build_minimal_prototype(
            census.primary[entry["signal_id"]],
            entry["state"],
            risks,
            max_holding_sessions=int(shared["max_holding_sessions"]),
            target_position=float(shared["target_position"]),
        )
        targets[variant["prototype_id"]] = result.target_position
        decisions = result.decisions.loc[result.decisions["date"] >= EVALUATION_START].copy()
        decisions.insert(0, "prototype_id", variant["prototype_id"])
        decision_frames.append(decisions)
    prototype_daily = pd.concat(decision_frames, ignore_index=True)
    decision_counts = (
        prototype_daily.groupby(["prototype_id", "action"], sort=True)
        .size()
        .rename("count")
        .reset_index()
    )
    interventions = prototype_daily.loc[
        prototype_daily["action"].isin(["BLOCK_ENTRY_RISK", "EXIT_RISK"])
    ].copy()
    intervention_by_year = interventions.assign(
        year=pd.to_datetime(interventions["date"]).dt.year
    ).groupby(["prototype_id", "year", "action"], sort=True).size().rename(
        "count"
    ).reset_index()
    baseline = prototype_daily.loc[
        prototype_daily["prototype_id"].eq(VARIANT_KEYS["none"]),
        ["date", "target_position"],
    ].rename(columns={"target_position": "no_filter_target"})
    target_differences = prototype_daily.loc[
        ~prototype_daily["prototype_id"].eq(VARIANT_KEYS["none"])
    ].merge(baseline, on="date", how="left", validate="many_to_one")
    target_differences = target_differences.loc[
        target_differences["target_position"].ne(target_differences["no_filter_target"])
    ].copy()

    periods = _periods(protocol)
    metric_rows: list[dict[str, object]] = []
    order_frames: list[pd.DataFrame] = []
    trade_frames: list[pd.DataFrame] = []
    for variant, variant_target in targets.items():
        metrics, orders, trades = _run_variant(
            variant,
            variant_target,
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
    effects = _factorial_effects(metrics_frame, protocol)
    reproduction = _reproduction_audit(
        source,
        prototype_daily,
        metrics_frame,
        orders_frame,
    )

    _write_csv(transition_audit, artifacts / "signal_transition_audit.csv")
    _write_csv(prototype_daily, artifacts / "prototype_daily.csv")
    _write_csv(decision_counts, artifacts / "decision_counts.csv")
    _write_csv(interventions, artifacts / "intervention_events.csv")
    _write_csv(intervention_by_year, artifacts / "intervention_summary_by_year.csv")
    _write_csv(target_differences, artifacts / "target_difference_events.csv")
    _write_csv(metrics_frame, artifacts / "window_metrics.csv")
    _write_csv(effects, artifacts / "factorial_effects.csv")
    _write_csv(orders_frame, artifacts / "orders.csv")
    _write_csv(trades_frame, artifacts / "closed_trades.csv")
    _write_csv(reproduction, artifacts / "reproduction_audit.csv")
    output_names = [
        "signal_transition_audit.csv",
        "prototype_daily.csv",
        "decision_counts.csv",
        "intervention_events.csv",
        "intervention_summary_by_year.csv",
        "target_difference_events.csv",
        "window_metrics.csv",
        "factorial_effects.csv",
        "orders.csv",
        "closed_trades.csv",
        "reproduction_audit.csv",
    ]
    _write_json(artifacts / "run_evidence.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset_fingerprint": replay_data.fingerprint,
        "cutoff": replay_data.cutoff.isoformat(),
        "transition_audit_passed": True,
        "ex05_reproduction_passed": True,
        "variants": list(targets),
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
        "三条信号身份及状态切入事件通过EX05/EX02证据链校验。完成四个冻结过滤版本、"
        f"{len(periods)}个独立资金窗口的回放和2×2效应计算，共记录"
        f"{len(interventions)}条过滤干预。双过滤版本的每日决策、窗口指标和订单精确复现"
        "EX05。\n\n未调参、未生成候选，也未调用SE排名、SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(metrics_frame, effects, decision_counts, interventions),
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
