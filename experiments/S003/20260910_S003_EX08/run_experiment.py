from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_pairs import evaluate_paired_state_inventory


EXPERIMENT_ID = "20260910_S003_EX08"


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


def _verify_registration(current: list[dict], previous: list[dict]) -> None:
    prior = {str(item["hypothesis_id"]): item for item in previous}
    for item in current:
        source = prior[str(item["hypothesis_id"])]
        mapping = {
            "mechanism": "mechanism",
            "signal_name": "signal_name",
            "entry_state": "state_primary",
            "state_id": "state_id",
            "behavior_hash": "behavior_hash",
        }
        if any(item[left] != source[right] for left, right in mapping.items()):
            raise ValueError(f"{item['hypothesis_id']}: registration differs from EX05")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX08 may not mutate strategy lifecycle state")
    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    density = protocol["density"]
    hypothesis_dir = repo / "experiments" / str(dataset["hypothesis_experiment"])
    invalid_dir = repo / "experiments" / str(dataset["invalid_experiment"])
    hypothesis_manifest = validate_experiment_archive(hypothesis_dir)
    invalid_manifest = validate_experiment_archive(invalid_dir)
    if invalid_manifest.get("status") != "INVALID":
        raise ValueError("EX07 must remain marked INVALID")
    previous_protocol = _read(hypothesis_dir / "artifacts" / "protocol.json")
    _verify_registration(protocol["hypotheses"], previous_protocol["hypotheses"])
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    result = evaluate_paired_state_inventory(
        intraday.frames["15m"],
        intraday.frames["5m"],
        protocol["hypotheses"],
        symbol=str(target["symbol"]),
        evaluation_start=str(dataset["evaluation_start"]),
        warmup_bars=int(dataset["warmup_bars"]),
        baseline_one_way_cost=float(execution["baseline_one_way_cost"]),
        stress_one_way_cost=float(execution["stress_one_way_cost"]),
        base_fraction=float(execution["base_fraction"]),
        density_window_sessions=int(density["window_sessions"]),
        median_episodes_required=int(density["median_closed_episodes_required"]),
        p10_episodes_required=int(density["p10_closed_episodes_required"]),
        force_close_unpaired_entries=True,
    )
    for name, frame in {
        "episodes.csv.gz": result.episodes,
        "paired_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "account_metrics.csv": result.account,
    }.items():
        compression = (
            {"method": "gzip", "compresslevel": 9, "mtime": 0}
            if name.endswith(".gz")
            else None
        )
        frame.to_csv(
            artifacts / name,
            index=False,
            encoding="utf-8-sig",
            compression=compression,
            lineterminator="\n",
        )
    reason_counts = result.episodes["exit_reason"].value_counts().sort_index().to_dict()
    counts = result.metrics["evidence"].value_counts().sort_index().to_dict()
    feasible = int((result.metrics["evidence"] == "FEASIBLE").sum())
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "hypothesis_protocol_sha": hypothesis_manifest["files"]["artifacts/protocol.json"]["sha256"],
        "invalid_predecessor_status": invalid_manifest["status"],
        "evaluated_pairs": len(result.metrics),
        "evidence_counts": {str(key): int(value) for key, value in counts.items()},
        "exit_reason_counts": {str(key): int(value) for key, value in reason_counts.items()},
        "signal_generation_failures": list(result.failures),
        "route_decision": "CONTINUE_TO_PROTOTYPE" if feasible else "STOP_NATIVE_DEFAULT_ROUTE",
        "candidate_created": False,
    }
    _write(artifacts / "paired_summary.json", summary)
    table = [
        "|机制|进→出|笔数|30日中位/P10|强平仓占比|基准均值/PF|压力均值/PF|标签|",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in result.metrics.sort_values(
        ["density_pass", "baseline_mean_return"], ascending=[False, False]
    ).itertuples(index=False):
        episodes = result.episodes.loc[result.episodes["hypothesis_id"] == row.hypothesis_id]
        forced_share = float(episodes["exit_reason"].eq("forced_close").mean())
        table.append(
            f"|{row.hypothesis_id}|{row.entry_state}→{row.exit_state}|{row.episodes}|"
            f"{row.rolling_30d_median_episodes:.0f}/{row.rolling_30d_p10_episodes:.0f}|"
            f"{forced_share:.1%}|{row.baseline_mean_return:.3%}/{row.baseline_profit_factor:.2f}|"
            f"{row.stress_mean_return:.3%}/{row.stress_profit_factor:.2f}|{row.evidence}|"
        )
    rate_pass = int(result.metrics["density_pass"].sum())
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        f"状态：COMPLETE。所有可执行入场均进入样本；7个状态对中{rate_pass}个满足证据速度，"
        f"{feasible}个达到FEASIBLE。未出现配对退出的交易均按15:00强制退出。\n",
        encoding="utf-8",
    )
    decision = (
        "存在FEASIBLE状态对，下一步只围绕这些结果形成唯一简化原型。"
        if feasible
        else "修正幸存者偏差后没有状态对达到可行标准，S003当前CZSC原生默认信号路线停止。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX08 结论\n\n"
        + "\n".join(table)
        + "\n\n"
        + decision
        + " 本实验没有创建候选，也没有修改SM或PTE。\n",
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
