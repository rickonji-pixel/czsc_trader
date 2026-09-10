from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_pairs import evaluate_paired_state_inventory


EXPERIMENT_ID = "20260910_S003_EX07"


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
        fields = {
            "mechanism": "mechanism",
            "signal_name": "signal_name",
            "entry_state": "state_primary",
            "state_id": "state_id",
            "behavior_hash": "behavior_hash",
        }
        for current_field, prior_field in fields.items():
            if item[current_field] != source[prior_field]:
                raise ValueError(
                    f"{item['hypothesis_id']}: {current_field} differs from EX05"
                )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in (
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX07 may not mutate strategy lifecycle state")
    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    density = protocol["density"]
    hypothesis_dir = repo / "experiments" / str(dataset["hypothesis_experiment"])
    hypothesis_manifest = validate_experiment_archive(hypothesis_dir)
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
        force_close_unpaired_entries=False,
    )
    outputs = {
        "episodes.csv.gz": result.episodes,
        "paired_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "account_metrics.csv": result.account,
    }
    for name, frame in outputs.items():
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
    counts = result.metrics["evidence"].value_counts().sort_index().to_dict()
    feasible = int((result.metrics["evidence"] == "FEASIBLE").sum())
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "INVALID",
        "hypothesis_protocol_sha": hypothesis_manifest["files"]["artifacts/protocol.json"][
            "sha256"
        ],
        "evaluated_pairs": len(result.metrics),
        "evidence_counts": {str(key): int(value) for key, value in counts.items()},
        "signal_generation_failures": list(result.failures),
        "route_decision": "INVALID_SURVIVORSHIP_BIAS",
        "candidate_created": False,
    }
    _write(artifacts / "paired_summary.json", summary)
    table = [
        "|机制|进→出|笔数|30日中位/P10|持有中位分钟|基准均值/PF|压力均值/PF|标签|",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in result.metrics.sort_values(
        ["density_pass", "baseline_mean_return"], ascending=[False, False]
    ).itertuples(index=False):
        table.append(
            f"|{row.hypothesis_id}|{row.entry_state}→{row.exit_state}|{row.episodes}|"
            f"{row.rolling_30d_median_episodes:.0f}/{row.rolling_30d_p10_episodes:.0f}|"
            f"{row.median_holding_minutes:.0f}|{row.baseline_mean_return:.3%}/"
            f"{row.baseline_profit_factor:.2f}|{row.stress_mean_return:.3%}/"
            f"{row.stress_profit_factor:.2f}|{row.evidence}|"
        )
    rate_pass = int(result.metrics["density_pass"].sum())
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        f"状态：INVALID。成功重建全部成对信号状态，7个固定状态对中{rate_pass}个满足"
        f"证据速度，表面上{feasible}个达到FEASIBLE；实现只保留同日出现退出状态的入场，"
        "遗漏了未出现退出状态的路径，存在幸存者偏差。\n",
        encoding="utf-8",
    )
    decision = "本实验存在幸存者偏差，全部绩效与路线裁决作废。"
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX07 结论\n\n"
        + "\n".join(table)
        + "\n\n"
        + decision
        + " 具体原因是只有同日后来出现退出状态的入场才被保留；未出现退出状态的入场被"
        "事后删除。EX08将强制所有可执行入场进入样本，无配对退出时按15:00收盘退出。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "INVALID",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
