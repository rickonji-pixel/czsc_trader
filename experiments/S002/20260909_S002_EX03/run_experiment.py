from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from czsc_trader.application.context import RepositoryContext
from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260909_S002_EX03"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def _duplicate_map(redundancy: dict[str, object]) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for index, group in enumerate(redundancy["duplicate_groups"], start=1):
        members = sorted(
            group["members"],
            key=lambda item: (item["frequency"], item["name"], item["state_id"]),
        )
        canonical = str(members[0]["state_id"])
        for member in members:
            result[str(member["state_id"])] = (index, canonical)
    return result


def _review_universe(
    metrics: pd.DataFrame,
    redundancy: dict[str, object],
    review: dict[str, object],
) -> tuple[pd.DataFrame, dict[str, int]]:
    horizons = {int(value) for value in review["horizons"]}
    scoped = metrics.loc[
        metrics["horizon"].isin(horizons)
        & metrics["evidence_quality"].eq(review["required_evidence_quality"])
        & ~metrics["state_primary"].isin(review["excluded_primary_states"])
    ].copy()
    duplicate_lookup = _duplicate_map(redundancy)
    rows = []
    before_deduplication = 0
    for state_id, group in scoped.groupby("state_id", sort=True):
        if set(group["horizon"].astype(int)) != horizons:
            continue
        means = group.set_index("horizon")["net_return_mean"].astype(float)
        same_direction = means.gt(0).all() or means.lt(0).all()
        minimum_consistency = float(group["year_sign_consistency"].min())
        if bool(review["require_same_mean_direction"]) and not same_direction:
            continue
        if minimum_consistency < float(review["minimum_year_sign_consistency"]):
            continue
        before_deduplication += 1
        duplicate_group, canonical = duplicate_lookup.get(str(state_id), (0, str(state_id)))
        if bool(review["collapse_exact_behavior_duplicates"]) and canonical != state_id:
            continue
        first = group.iloc[0]
        by_horizon = group.set_index("horizon")
        rows.append({
            "state_id": state_id,
            "frequency": first["frequency"],
            "name": first["name"],
            "namespace": first["namespace"],
            "state_primary": first["state_primary"],
            "forward_bias": "POSITIVE" if means.gt(0).all() else "NEGATIVE",
            "minimum_event_count": int(group["event_count"].min()),
            "coverage_years": int(group["coverage_years"].min()),
            "minimum_year_sign_consistency": minimum_consistency,
            "median_absolute_standardized_effect": float(group["standardized_effect"].abs().median()),
            "net_return_h3": float(by_horizon.at[3, "net_return_mean"]),
            "net_return_h5": float(by_horizon.at[5, "net_return_mean"]),
            "net_return_h10": float(by_horizon.at[10, "net_return_mean"]),
            "mfe_h5": float(by_horizon.at[5, "mfe_mean"]),
            "mae_h5": float(by_horizon.at[5, "mae_mean"]),
            "exact_duplicate_group": duplicate_group,
        })
    result = pd.DataFrame(rows).sort_values(
        ["minimum_year_sign_consistency", "median_absolute_standardized_effect", "minimum_event_count"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    result.insert(0, "mechanical_rank", np.arange(1, len(result) + 1))
    return result, {
        "eligible_before_exact_deduplication": before_deduplication,
        "eligible_after_exact_deduplication": len(result),
    }


def _resolve_review(
    reviews: list[dict[str, object]],
    metrics: pd.DataFrame,
    universe: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    eligible_ids = set(universe["state_id"])
    for review in reviews:
        matched = metrics.loc[
            metrics["frequency"].eq(review["frequency"])
            & metrics["name"].eq(review["name"])
            & metrics["state_primary"].eq(review["state"])
        ].copy()
        state_ids = matched["state_id"].drop_duplicates().tolist()
        if len(state_ids) != 1:
            raise ValueError(f"semantic review does not resolve uniquely: {review}")
        state_id = str(state_ids[0])
        by_horizon = matched.set_index("horizon")
        rows.append({
            "state_id": state_id,
            "frequency": review["frequency"],
            "name": review["name"],
            "state_primary": review["state"],
            "decision": review["decision"],
            "role": review["role"],
            "hypothesis": review.get("hypothesis", ""),
            "mechanical_eligible": state_id in eligible_ids,
            "event_count_h5": int(by_horizon.at[5, "event_count"]),
            "coverage_years_h5": int(by_horizon.at[5, "coverage_years"]),
            "year_sign_consistency_h3": float(by_horizon.at[3, "year_sign_consistency"]),
            "year_sign_consistency_h5": float(by_horizon.at[5, "year_sign_consistency"]),
            "year_sign_consistency_h10": float(by_horizon.at[10, "year_sign_consistency"]),
            "net_return_h3": float(by_horizon.at[3, "net_return_mean"]),
            "net_return_h5": float(by_horizon.at[5, "net_return_mean"]),
            "net_return_h10": float(by_horizon.at[10, "net_return_mean"]),
            "mfe_h5": float(by_horizon.at[5, "mfe_mean"]),
            "mae_h5": float(by_horizon.at[5, "mae_mean"]),
            "reason": review["reason"],
        })
    result = pd.DataFrame(rows)
    selected = result.loc[result["decision"] == "SELECT"]
    if selected.empty or not selected["mechanical_eligible"].all():
        raise ValueError("every selected hypothesis must belong to the frozen mechanical review universe")
    return result


def _overlap(selected_events: pd.DataFrame, trading_dates: pd.DatetimeIndex) -> pd.DataFrame:
    position = {date: index for index, date in enumerate(trading_dates)}
    event_sets = {
        state_id: {position[date] for date in pd.to_datetime(group["signal_date"]).dt.normalize() if date in position}
        for state_id, group in selected_events.groupby("state_id")
    }
    rows = []
    ids = sorted(event_sets)
    for left_index, left in enumerate(ids):
        for right in ids[left_index + 1 :]:
            left_events = event_sets[left]
            right_events = event_sets[right]
            rows.append({
                "left_state_id": left,
                "right_state_id": right,
                "left_events": len(left_events),
                "right_events": len(right_events),
                "same_day_events": len(left_events & right_events),
                "left_within_5_sessions_of_right": sum(any(abs(value - other) <= 5 for other in right_events) for value in left_events),
                "right_within_5_sessions_of_left": sum(any(abs(value - other) <= 5 for other in left_events) for value in right_events),
            })
    return pd.DataFrame(rows)


def _review_chart(
    daily: pd.DataFrame,
    selected_events: pd.DataFrame,
    reviews: pd.DataFrame,
    path: Path,
) -> None:
    prices = daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.loc[prices["dt"] >= pd.Timestamp("2021-01-04")].set_index("dt")
    figure = go.Figure(
        go.Candlestick(
            x=prices.index,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            name="日K",
            increasing_line_color="#ef4444",
            decreasing_line_color="#22c55e",
        )
    )
    palette = ["#60a5fa", "#a78bfa", "#fb7185", "#f59e0b"]
    for color, (_, review) in zip(palette, reviews.loc[reviews["decision"] == "SELECT"].iterrows()):
        events = selected_events.loc[selected_events["state_id"] == review["state_id"]].copy()
        dates = pd.to_datetime(events["signal_date"]).dt.normalize()
        events = events.loc[dates.isin(prices.index)].copy()
        dates = pd.to_datetime(events["signal_date"]).dt.normalize()
        risk = review["role"] == "RISK_FILTER"
        y = prices.loc[dates, "high"].to_numpy() * 1.025 if risk else prices.loc[dates, "low"].to_numpy() * 0.975
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=y,
                mode="markers",
                marker={"symbol": "triangle-down" if risk else "triangle-up", "size": 9, "color": color},
                name=f"{review['role']} · {review['state_primary']}",
                customdata=np.column_stack([
                    events["name"].astype(str),
                    events["state_full"].fillna("").astype(str),
                    np.repeat(review["hypothesis"], len(events)),
                ]),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>%{customdata[0]}<br>状态 %{customdata[1]}"
                    "<br>假设 %{customdata[2]}<extra></extra>"
                ),
            )
        )
    figure.update_layout(
        template="plotly_dark",
        title="510500.SH | S002 EX03 信号假设复核 | 2021.01.04 - 2026.09.08",
        height=760,
        margin={"l": 60, "r": 30, "t": 80, "b": 45},
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )
    figure.write_html(
        path,
        include_plotlyjs=True,
        full_html=True,
        config={"responsive": True, "displaylogo": False},
    )


def _render_conclusion(
    counts: dict[str, int],
    reviews: pd.DataFrame,
    overlap: pd.DataFrame,
) -> str:
    selected = reviews.loc[reviews["decision"] == "SELECT"]
    deferred = reviews.loc[reviews["decision"] == "DEFER"]
    max_same_day = int(overlap["same_day_events"].max()) if not overlap.empty else 0
    lines = [
        f"# {EXPERIMENT_ID} 结论",
        "",
        "状态：COMPLETE。EX02探索结果已收敛为四条S002研究假设。",
        "",
        "## 收敛结果",
        "",
        f"机械规则筛得{counts['eligible_before_exact_deduplication']}个状态；精确去重后保留"
        f"{counts['eligible_after_exact_deduplication']}个进入人工阅读范围。机械筛选仍然过宽，"
        "不能直接用于组合搜索。",
        "",
        "| 角色 | 频率 | 信号状态 | 3日净收益 | 5日净收益 | 10日净收益 | 5日事件 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for _, row in selected.iterrows():
        lines.append(
            f"| {row['role']} | {row['frequency']} | `{row['name']}::{row['state_primary']}` | "
            f"{row['net_return_h3']:.2%} | {row['net_return_h5']:.2%} | "
            f"{row['net_return_h10']:.2%} | {int(row['event_count_h5'])} |"
        )
    lines.extend([
        "",
        f"四条假设两两之间同日触发最多{max_same_day}次，且均不是EX02精确重复状态。"
        "这支持它们具有事件互补性，但尚未证明组合后能提高策略表现。",
        "",
        "## 暂缓项",
        "",
    ])
    for _, row in deferred.iterrows():
        lines.append(f"- `{row['name']}::{row['state_primary']}`：{row['reason']}。")
    lines.extend([
        "",
        "## 研究判断",
        "",
        "本轮最重要的结果是把入场拆成均值回归与趋势延续两种不同机制，并为两者提供统一的"
        "趋势风险和结构压力过滤假设。四条信号不应直接加权求和；下一轮应分别构造两个简洁"
        "原型，使用相同风险过滤和退出口径比较，再决定是否需要组合。",
        "",
        "所有效果数字均来自已被阅读的开发池，属于假设形成证据。EX04必须预注册原型规则，"
        "通过滚动或分段回放观察稳定性；最终可信度仍来自冻结后的PTE前瞻观察。",
        "",
        "本轮未生成候选、未运行策略回测，也未修改SM或PTE。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "parameter_search", "backtest_allowed", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("hypothesis review may not backtest, promote, or deploy")

    source = repo_root / "experiments" / str(protocol["source_experiment"]["experiment_id"])
    validate_experiment_archive(source)
    expected = protocol["source_experiment"]
    source_files = {
        "manifest_sha256": source / "experiment_manifest.json",
        "event_metrics_sha256": source / "artifacts" / "event_metrics.csv",
        "signal_events_sha256": source / "artifacts" / "signal_events.csv.gz",
    }
    for key, path in source_files.items():
        if _sha256(path) != str(expected[key]):
            raise ValueError(f"source evidence hash differs: {path.name}")

    metrics = pd.read_csv(source / "artifacts" / "event_metrics.csv")
    stability = pd.read_csv(source / "artifacts" / "signal_stability.csv")
    events = pd.read_csv(source / "artifacts" / "signal_events.csv.gz")
    redundancy = _read_json(source / "artifacts" / "redundancy_map.json")
    universe, counts = _review_universe(metrics, redundancy, protocol["mechanical_review"])
    reviews = _resolve_review(protocol["semantic_reviews"], metrics, universe)
    selected_ids = set(reviews.loc[reviews["decision"] == "SELECT", "state_id"])
    selected_metrics = metrics.loc[metrics["state_id"].isin(selected_ids)].copy()
    selected_stability = stability.loc[stability["state_id"].isin(selected_ids)].copy()
    selected_events = events.loc[events["state_id"].isin(selected_ids)].copy()

    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    target = protocol["research_target"]
    market = load_market_data(
        context.raw_dir,
        str(target["symbol"]),
        str(target["asset_type"]),
        cutoff=pd.Timestamp(target["development_cutoff"]),
    )
    trading_dates = pd.DatetimeIndex(pd.to_datetime(market.daily["dt"]).dt.normalize())
    overlap = _overlap(selected_events, trading_dates)

    selected_events = selected_events.merge(
        market.daily.assign(signal_date=pd.to_datetime(market.daily["dt"]).dt.strftime("%Y-%m-%d"))[
            ["signal_date", "open", "high", "low", "close"]
        ],
        on="signal_date",
        how="left",
        validate="many_to_one",
    )
    hypotheses = []
    for _, row in reviews.loc[reviews["decision"] == "SELECT"].iterrows():
        hypotheses.append({
            "hypothesis_id": f"H{len(hypotheses) + 1}",
            "state_id": row["state_id"],
            "frequency": row["frequency"],
            "signal_name": row["name"],
            "state_primary": row["state_primary"],
            "role": row["role"],
            "mechanism": row["hypothesis"],
            "interpretation": row["reason"],
            "status": "EXPLORATORY",
        })

    _write_csv(universe, artifacts / "mechanical_review_universe.csv")
    _write_csv(reviews, artifacts / "semantic_review.csv")
    _write_csv(selected_metrics, artifacts / "selected_event_metrics.csv")
    _write_csv(selected_stability, artifacts / "selected_annual_metrics.csv")
    _write_csv(selected_events, artifacts / "selected_events.csv")
    _write_csv(overlap, artifacts / "selected_event_overlap.csv")
    _write_json(artifacts / "hypotheses.json", {"schema_version": 1, "hypotheses": hypotheses})
    _review_chart(market.daily, selected_events, reviews, artifacts / "signal_review.html")

    output_names = [
        "mechanical_review_universe.csv",
        "semantic_review.csv",
        "selected_event_metrics.csv",
        "selected_annual_metrics.csv",
        "selected_events.csv",
        "selected_event_overlap.csv",
        "hypotheses.json",
        "signal_review.html",
    ]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "source_experiment": expected,
        "mechanical_review_counts": counts,
        "selected_hypotheses": len(hypotheses),
        "selected_event_count": int(len(selected_events)),
        "selected_same_day_overlap_max": int(overlap["same_day_events"].max()),
        "market_data": {
            "symbol": market.symbol,
            "cutoff": str(target["development_cutoff"]),
            "manifest_sha256": _sha256(context.raw_dir / "510500_manifest.json"),
        },
        "outputs": {name: {"bytes": (artifacts / name).stat().st_size, "sha256": _sha256(artifacts / name)} for name in output_names},
    }
    _write_json(artifacts / "run_evidence.json", evidence)
    (experiment_dir / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        f"按冻结规则从EX02筛得{counts['eligible_before_exact_deduplication']}个状态，精确去重后"
        f"保留{counts['eligible_after_exact_deduplication']}个机械复核项。完成人工语义审查、"
        f"四条假设的全期限与逐年证据提取、{len(selected_events)}次触发事件定位、事件重叠审计"
        "和交互式K线图生成。\n\n未生成候选、未运行策略回测，也未调用SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(counts, reviews, overlap), encoding="utf-8"
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
