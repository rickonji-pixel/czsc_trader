from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX51"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_post_signal_prices"):
        raise ValueError("invalid frozen protocol")
    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    for path, expected in (
        (source / "experiment_manifest.json", source_spec["manifest_sha256"]),
        (source / "artifacts/combination_states.csv.gz", source_spec["states_sha256"]),
        (source / "artifacts/combination_evidence.json", source_spec["evidence_sha256"]),
    ):
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")

    frame = pd.read_csv(source / "artifacts/combination_states.csv.gz", parse_dates=["dt"]).set_index("dt")
    enter = frame["core_up"] & frame["confirmation_up"] & ~frame["internal_fragility"]
    leave = frame["core_down"]
    active = False
    entry: pd.Timestamp | None = None
    rows: list[dict[str, object]] = []
    states: list[bool] = []
    for position, date in enumerate(frame.index):
        if not active and bool(enter.iloc[position]):
            active = True
            entry = date
        elif active and bool(leave.iloc[position]):
            rows.append({
                "entry_date": entry,
                "exit_date": date,
                "holding_sessions": position - int(frame.index.get_loc(entry)) + 1,
            })
            active = False
            entry = None
        states.append(active)
    if active:
        rows.append({"entry_date": entry, "exit_date": pd.NaT, "holding_sessions": np.nan})
    cycles = pd.DataFrame(rows)
    entries = pd.Series(False, index=frame.index)
    entries.loc[pd.DatetimeIndex(cycles["entry_date"])] = True
    density = protocol["density"]
    rolling = entries.astype(int).rolling(
        int(density["rolling_window_sessions"]),
        min_periods=int(density["rolling_window_sessions"]),
    ).sum().dropna()
    complete = cycles.loc[cycles["exit_date"].notna()]
    metrics = {
        "independent_cycles": int(len(cycles)),
        "complete_cycles": int(len(complete)),
        "rolling_60_median": float(rolling.median()),
        "rolling_60_p10": float(rolling.quantile(0.1)),
        "rolling_60_minimum": float(rolling.min()),
        "state_exposure": float(pd.Series(states, index=frame.index).mean()),
        "median_holding_sessions": float(complete["holding_sessions"].median()),
        "risk_blocked_entry_sessions": int((frame["core_up"] & frame["confirmation_up"] & frame["internal_fragility"]).sum()),
    }
    passed = bool(
        metrics["rolling_60_median"] >= float(density["minimum_median"])
        and metrics["rolling_60_p10"] >= float(density["minimum_p10"])
    )
    decision = "PROCEED_TO_FIXED_COMBINATION_RETURN_TEST" if passed else "STOP_ENTRY_GATE_COMBINATION_ON_DENSITY"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "metrics": metrics,
        "checks": {
            "rolling_60_median": metrics["rolling_60_median"] >= float(density["minimum_median"]),
            "rolling_60_p10": metrics["rolling_60_p10"] >= float(density["minimum_p10"]),
        },
        "decision": decision,
        "reads_post_signal_prices": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    for column in ("entry_date", "exit_date"):
        cycles[column] = pd.to_datetime(cycles[column]).dt.strftime("%Y-%m-%d")
    cycles.to_csv(artifacts / "cycles.csv", index=False, lineterminator="\n")
    (artifacts / "density_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX51 执行\n\n"
        f"状态：`COMPLETE`。完整组合形成{metrics['independent_cycles']}个周期；滚动60日"
        f"中位数/P10为{metrics['rolling_60_median']:.1f}/{metrics['rolling_60_p10']:.1f}。"
        "本轮没有读取信号后行情或收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX51 结论\n\n"
        f"裁决：`{decision}`。风险层阻止了{metrics['risk_blocked_entry_sessions']}个潜在入场日，"
        f"完整组合暴露率{metrics['state_exposure']:.2%}、持有中位数"
        f"{metrics['median_holding_sessions']:.1f}个交易日；频率门{'通过' if passed else '失败'}。"
        "本轮没有创建候选，没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
