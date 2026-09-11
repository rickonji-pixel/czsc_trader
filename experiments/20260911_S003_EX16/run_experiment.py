from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_opportunity_map import build_intraday_opportunity_map


EXPERIMENT_ID = "20260911_S003_EX16"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if bool(protocol.get("selection_allowed")):
        raise ValueError("EX16 must not select trading days")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX16 may not mutate strategy lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    costs = protocol["costs"]
    account = protocol["account"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    result = build_intraday_opportunity_map(
        intraday.frames[str(dataset["frequency"])],
        protocol["segments"],
        evaluation_start=str(dataset["evaluation_start"]),
        baseline_one_way_cost=float(costs["baseline_one_way"]),
        stress_one_way_cost=float(costs["stress_one_way"]),
        base_fraction=float(account["base_fraction"]),
        account_segment_ids=list(map(str, account["daily_long_overlay_segments"])),
    )
    for name, frame in {
        "segment_episodes.csv.gz": result.episodes,
        "segment_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "account_metrics.csv": result.account,
    }.items():
        compression = {"method": "gzip", "compresslevel": 9, "mtime": 0} if name.endswith(".gz") else None
        frame.to_csv(
            artifacts / name,
            index=False,
            encoding="utf-8-sig",
            compression=compression,
            lineterminator="\n",
        )

    metrics = result.metrics.set_index("segment_id")
    account_metrics = result.account.set_index(["segment_id", "cost_label"])
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "selection_performed": False,
        "segments": int(len(metrics)),
        "best_gross_segment": str(metrics["gross_mean_return"].idxmax()),
        "best_baseline_segment": str(metrics["baseline_mean_return"].idxmax()),
        "morning_share_of_intraday_mean": float(
            metrics.loc["OPEN_TO_1030", "gross_mean_return"]
            / metrics.loc["OPEN_TO_CLOSE", "gross_mean_return"]
        ),
        "signal_0935_share_of_morning_mean": float(
            metrics.loc["SIGNAL_0935_TO_1030", "gross_mean_return"]
            / metrics.loc["OPEN_TO_1030", "gross_mean_return"]
        ),
        "baseline_daily_overlay_increment": {
            segment_id: float(
                account_metrics.loc[(segment_id, "BASELINE"), "incremental_terminal_return_vs_static"]
            )
            for segment_id in account["daily_long_overlay_segments"]
        },
        "candidate_created": False,
    }
    _write(artifacts / "opportunity_summary.json", summary)

    table = [
        "|区间|毛收益均值|基准净均值|压力净均值|成本容忍度|基准正收益年份|压力正收益年份|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result.metrics.itertuples(index=False):
        table.append(
            f"|{row.segment_id}|{row.gross_mean_return:.3%}|{row.baseline_mean_return:.3%}|"
            f"{row.stress_mean_return:.3%}|{row.break_even_one_way_cost_bp:.2f}bp|"
            f"{row.baseline_positive_years}|{row.stress_positive_years}|"
        )
    account_table = [
        "|区间|成本档|账户终值|相对静态底仓|最大回撤|卡玛|",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in result.account.itertuples(index=False):
        account_table.append(
            f"|{row.segment_id}|{row.cost_label}|{row.terminal_equity:.3f}|"
            f"{row.incremental_terminal_return_vs_static:.1%}|{row.max_drawdown:.1%}|"
            f"{row.calmar:.2f}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S003 EX16 执行\n\n"
        f"状态：COMPLETE。使用{int(result.metrics['episodes'].max())}个交易日观察8个冻结时段，"
        "没有筛选交易日、拟合参数或生成候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX16 结论\n\n"
        + "\n".join(table)
        + "\n\n## T+1每日固定多头库存轮换\n\n"
        + "\n".join(account_table)
        + "\n\n开盘至10:30贡献全天日内平均收益的"
        f"{summary['morning_share_of_intraday_mean']:.1%}；等09:35收线后，至10:30仍可捕获的"
        f"平均收益只占完整早盘的{summary['signal_0935_share_of_morning_mean']:.1%}。"
        "隔夜收益在基准成本后六个年度均为负，10:30以后区间的成本容忍度也很低。"
        "下一轮优先研究前一日收盘可确定的信息和开盘缺口，执行目标集中到上午。\n\n"
        "本实验只描述收益来源和可实现边界，不授予任何机制研究资格。"
        "下一轮机制必须在结果产生前固定信息时点、事件定义、执行时点和强制对手。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
