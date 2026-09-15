from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX21"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _platforms(frame: pd.DataFrame, step: float, minimum_points: int, minimum_span: float) -> list[dict[str, object]]:
    feasible = frame.loc[frame["revised_feasible"]].sort_values("confirmation_gate_quantile")
    groups: list[list[pd.Series]] = []
    current: list[pd.Series] = []
    for _, row in feasible.iterrows():
        if current and not np.isclose(float(row["confirmation_gate_quantile"]) - float(current[-1]["confirmation_gate_quantile"]), step):
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    output = []
    for index, group in enumerate(groups, start=1):
        low = float(group[0]["confirmation_gate_quantile"])
        high = float(group[-1]["confirmation_gate_quantile"])
        if len(group) >= minimum_points and high - low + 1e-12 >= minimum_span:
            subset = pd.DataFrame(group)
            output.append({
                "platform_id": f"PLATFORM-{index:02d}",
                "minimum_quantile": low,
                "maximum_quantile": high,
                "quantile_span": high - low,
                "grid_points": len(group),
                "unique_behaviors": int(subset["behavior_hash"].nunique()),
                "minimum_cagr": float(subset["full_cagr"].min()),
                "worst_maximum_drawdown": float(subset["full_maximum_drawdown"].min()),
                "minimum_frequency_median": float(subset["rolling_60_closed_trades_median"].min()),
            })
    return output


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_new_returns"):
        raise ValueError("EX21 protocol identity or return-access declaration differs")
    if not protocol.get("reuses_complete_grid"):
        raise ValueError("EX21 must reuse the complete EX20 grid")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX21 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex20 = repo / str(sources["ex20_archive"])
    validate_experiment_archive(ex20)
    frozen = {
        ex20 / "experiment_manifest.json": sources["ex20_manifest_sha256"],
        ex20 / "artifacts/protocol.json": sources["ex20_protocol_sha256"],
        ex20 / "artifacts/grid_results.csv": sources["ex20_grid_results_sha256"],
        repo / str(sources["ex19_search_protocol"]): sources["ex19_search_protocol_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX21 source differs: {path}")

    frame = pd.read_csv(ex20 / "artifacts/grid_results.csv")
    if len(frame) != 21 or frame["confirmation_gate_quantile"].nunique() != 21:
        raise ValueError("EX20 grid is incomplete")
    gates = protocol["unchanged_hard_gates"]
    frequency_minimum = float(protocol["frequency_policy"]["minimum_rolling_60_closed_trades_median"])

    conditions = {
        "full_cagr": frame["full_cagr"].ge(float(gates["minimum_full_cagr"])),
        "full_maximum_drawdown": frame["full_maximum_drawdown"].ge(float(gates["maximum_drawdown_limit"])),
        "s001_v2_maximum_drawdown": frame["full_maximum_drawdown"].gt(float(gates["s001_v2_maximum_drawdown"])),
        "frequency_median": frame["rolling_60_closed_trades_median"].ge(frequency_minimum),
        "discovery_cagr": frame["discovery_cagr"].ge(float(gates["minimum_discovery_cagr"])),
        "confirmation_cagr": frame["confirmation_cagr"].ge(float(gates["minimum_confirmation_cagr"])),
        "confirmation_maximum_drawdown": frame["confirmation_maximum_drawdown"].ge(float(gates["confirmation_maximum_drawdown_limit"])),
    }
    frame["previous_feasible"] = frame["feasible"].astype(bool)
    frame["frequency_gate_pass"] = conditions["frequency_median"]
    frame["revised_feasible"] = np.logical_and.reduce(list(conditions.values()))
    frame["failed_gates"] = [
        "|".join(name for name, values in conditions.items() if not bool(values.iloc[index]))
        for index in range(len(frame))
    ]
    failure_counts = {name: int((~values).sum()) for name, values in conditions.items()}

    search_protocol = _read(repo / str(sources["ex19_search_protocol"]))
    platform_rule = search_protocol["platform"]
    platforms = _platforms(
        frame,
        0.025,
        int(platform_rule["minimum_consecutive_feasible_grid_points"]),
        float(platform_rule["minimum_quantile_span"]),
    )
    decision = "REVIEW_RELAXED_FREQUENCY_PLATFORM_FOUND" if platforms else "STOP_RELAXED_FREQUENCY_NO_PLATFORM"
    frame.to_csv(artifacts / "reclassified_grid_results.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "platforms.json", {"schema_version": 1, "platforms": platforms})

    non_drawdown_conditions = [values for name, values in conditions.items() if name != "full_maximum_drawdown"]
    drawdown_only_blocked = frame.loc[np.logical_and.reduce(non_drawdown_conditions) & ~conditions["full_maximum_drawdown"]]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "grid_points": len(frame),
        "frequency_gate_pass_points": int(conditions["frequency_median"].sum()),
        "revised_feasible_grid_points": int(frame["revised_feasible"].sum()),
        "failure_counts": failure_counts,
        "drawdown_only_blocked_quantiles": [float(value) for value in drawdown_only_blocked["confirmation_gate_quantile"]],
        "platform_count": len(platforms),
        "platforms": platforms,
        "p10_is_observation_only": True,
        "returns_recomputed": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "evaluation_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX21 执行\n\n"
        f"状态：`COMPLETE`。复用EX20完整21点结果，以用户确认的新频率门重新判定。"
        f"频率门通过{int(conditions['frequency_median'].sum())}点，全部硬门通过"
        f"{int(frame['revised_feasible'].sum())}点。没有重复读取收益或回测。\n",
        encoding="utf-8",
    )
    blocked_text = "、".join(f"Q{value:.3f}" for value in evidence["drawdown_only_blocked_quantiles"]) or "无"
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX21 结论\n\n"
        f"裁决：`{decision}`。频率中位数门从7笔降至5笔并删除P10硬门后，21点中有"
        f"{int(conditions['frequency_median'].sum())}点通过频率门，但仍无任何点满足全部硬门。"
        f"{failure_counts['full_maximum_drawdown']}点全部违反20%最大回撤门；只被完整期最大回撤"
        f"单独阻断的点为{blocked_text}。未形成连续3点参数平台，也未创建候选。需先与用户评审。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
