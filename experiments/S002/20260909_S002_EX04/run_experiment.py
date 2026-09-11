from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.signal_hypotheses import (
    build_review_universe,
    event_overlap,
    resolve_semantic_reviews,
    write_review_chart,
)


EXPERIMENT_ID = "20260909_S002_EX04"


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
        "所有效果数字均来自已被阅读的开发池，属于假设形成证据。EX05必须预注册原型规则，"
        "通过滚动或分段回放观察稳定性；最终可信度仍来自冻结后的PTE前瞻观察。",
        "",
        "本轮未生成候选、未运行策略回测，也未修改SM或PTE。",
    ])
    return "\n".join(lines) + "\n"


def _validate_protocol(protocol: dict[str, object]) -> None:
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "candidate_generation",
        "parameter_search",
        "backtest_allowed",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("hypothesis review may not backtest, promote, or deploy")


def _validate_source(repo_root: Path, expected: dict[str, object]) -> Path:
    source = repo_root / "experiments" / str(expected["experiment_id"])
    validate_experiment_archive(source)
    source_files = {
        "manifest_sha256": source / "experiment_manifest.json",
        "event_metrics_sha256": source / "artifacts" / "event_metrics.csv",
        "signal_events_sha256": source / "artifacts" / "signal_events.csv.gz",
    }
    for key, path in source_files.items():
        if _sha256(path) != str(expected[key]):
            raise ValueError(f"source evidence hash differs: {path.name}")
    return source


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate_protocol(protocol)

    expected = protocol["source_experiment"]
    source = _validate_source(repo_root, expected)
    source_artifacts = source / "artifacts"
    metrics = pd.read_csv(source_artifacts / "event_metrics.csv")
    stability = pd.read_csv(source_artifacts / "signal_stability.csv")
    events = pd.read_csv(source_artifacts / "signal_events.csv.gz")
    redundancy = _read_json(source_artifacts / "redundancy_map.json")

    universe, counts = build_review_universe(
        metrics,
        redundancy,
        protocol["mechanical_review"],
    )
    reviews = resolve_semantic_reviews(
        protocol["semantic_reviews"],
        metrics,
        universe,
    )
    selected_ids = set(reviews.loc[reviews["decision"] == "SELECT", "state_id"])
    selected_metrics = metrics.loc[metrics["state_id"].isin(selected_ids)].copy()
    selected_stability = stability.loc[
        stability["state_id"].isin(selected_ids)
    ].copy()
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
    overlap = event_overlap(selected_events, trading_dates)
    selected_events = selected_events.merge(
        market.daily.assign(
            signal_date=pd.to_datetime(market.daily["dt"]).dt.strftime("%Y-%m-%d")
        )[["signal_date", "open", "high", "low", "close"]],
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
    _write_json(
        artifacts / "hypotheses.json",
        {"schema_version": 1, "hypotheses": hypotheses},
    )
    write_review_chart(
        market.daily,
        selected_events,
        reviews,
        artifacts / "signal_review.html",
        start="2021-01-04",
        end=str(target["development_cutoff"]),
        title="510500.SH | S002 EX04 信号假设复核 | 2021.01.04 - 2026.09.08",
    )

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
    max_same_day = int(overlap["same_day_events"].max()) if not overlap.empty else 0
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "source_experiment": expected,
        "mechanical_review_counts": counts,
        "selected_hypotheses": len(hypotheses),
        "selected_event_count": int(len(selected_events)),
        "selected_same_day_overlap_max": max_same_day,
        "market_data": {
            "symbol": market.symbol,
            "cutoff": str(target["development_cutoff"]),
            "manifest_sha256": _sha256(context.raw_dir / "510500_manifest.json"),
        },
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
        f"按冻结规则从EX02筛得{counts['eligible_before_exact_deduplication']}个状态，精确去重后"
        f"保留{counts['eligible_after_exact_deduplication']}个机械复核项。完成人工语义审查、"
        f"四条假设的全期限与逐年证据提取、{len(selected_events)}次触发事件定位、事件重叠审计"
        "和交互式K线图生成。\n\n未生成候选、未运行策略回测，也未调用SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(counts, reviews, overlap),
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
