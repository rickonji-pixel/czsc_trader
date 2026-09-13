from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_regime import classify_lagged_daily_regime
from czsc_trader.intraday_signal_census import generate_intraday_signal_census


EXPERIMENT_ID = "20260913_S001_EX15"


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


def _data_fingerprint(hashes: dict[str, str], protocol: dict[str, object]) -> str:
    payload = {
        "intraday_files": dict(sorted(hashes.items())),
        "protocol": protocol,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "reads_forward_returns",
        "assigns_trade_direction",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(bool(protocol.get(key)) for key in forbidden):
        raise ValueError("EX15 is a descriptive density census only")

    target = dict(protocol["research_target"])
    dataset = dict(protocol["dataset"])
    context = dict(protocol["daily_context"])
    registry = dict(protocol["signal_registry"])
    screen = dict(protocol["density_screen"])
    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    frame = loaded.frames[str(dataset["intraday_period"])]
    cutoff = pd.Timestamp(str(dataset["cutoff"]))
    if frame["Date"].max().normalize() != cutoff:
        raise ValueError("intraday cutoff differs from frozen protocol")

    one = loaded.frames["1m"].copy()
    one["trade_date"] = pd.to_datetime(one["Date"], errors="raise").dt.normalize()
    daily_close = one.groupby("trade_date", sort=True, observed=True)["Close"].last().astype(float)
    regimes = classify_lagged_daily_regime(
        daily_close,
        lookback=int(context["lookback"]),
        er_threshold=float(context["er_threshold"]),
    )
    result = generate_intraday_signal_census(
        frame,
        str(target["symbol"]),
        regimes["regime"],
        evaluation_start=str(dataset["evaluation_start"]),
        warmup_bars=int(registry["warmup_bars"]),
        window_sessions=int(screen["window_sessions"]),
        median_event_days_required=int(screen["median_event_days_required"]),
        p10_event_days_required=int(screen["p10_event_days_required"]),
        distinct_event_days_required=int(screen["distinct_event_days_required"]),
    )
    result.catalog.to_csv(artifacts / "signal_catalog.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    result.states.to_csv(artifacts / "state_density.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    result.events.to_csv(
        artifacts / "signal_events.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    _write(artifacts / "redundancy_map.json", result.redundancy)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "data_fingerprint": _data_fingerprint(loaded.hashes, protocol),
        "registry": result.summary,
        "density_screen": screen,
        "scope": {
            "forward_returns_read": False,
            "trade_direction_assigned": False,
            "candidate_created": False,
            "strategy_manager_mutated": False,
            "pte_mutated": False,
        },
    }
    _write(artifacts / "census_summary.json", summary)
    _write(
        artifacts / "run_evidence.json",
        {
            "intraday_manifest": loaded.manifest,
            "intraday_hashes": dict(sorted(loaded.hashes.items())),
            "failures": list(result.failures),
        },
    )

    generated = int(result.summary["generated_configurations"])
    failed = int(result.summary["failed_configurations"])
    states = int(result.summary["observed_primary_states"])
    capable = int(result.summary["density_capable_states"])
    unique = int(result.summary["unique_event_behaviors"])
    (experiment / "03_execution.md").write_text(
        "# S001 EX15 执行\n\n"
        f"状态：COMPLETE。成功运行{generated}个15分钟原生默认信号，失败{failed}个；"
        f"观察到{states}个主状态，其中{capable}个满足事件密度必要条件。"
        "全部状态事件已按交易日去重，未读取事件后收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX15 结论\n\n"
        f"15分钟CZSC目录形成{unique}种非冗余的按日事件行为；{capable}个状态通过预注册的"
        "事件密度必要条件。通过只代表事件供给充足，尚无收益、执行或候选资格。下一轮只从"
        "通过密度筛选、金融语义可解释且与S001基线空仓期有覆盖的状态中，预注册少量互补机制"
        "假设，再评价费后收益、跨年一致性、事件独立性和对S001实际交易频率的增量。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "strategy_version": target["source_version"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
