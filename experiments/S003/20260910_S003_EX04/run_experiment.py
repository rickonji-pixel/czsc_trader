from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_signal_census import generate_intraday_signal_census


EXPERIMENT_ID = "20260910_S003_EX04"


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


def _data_fingerprint(hashes: dict[str, str], regime_manifest: dict[str, object]) -> str:
    payload = {
        "intraday_files": dict(sorted(hashes.items())),
        "regime_manifest_files": regime_manifest["files"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
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
        raise ValueError("EX04 is a descriptive density census only")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    registry = protocol["signal_registry"]
    screen = protocol["density_screen"]
    regime_experiment = repo / "experiments" / str(dataset["regime_experiment"])
    regime_manifest = validate_experiment_archive(regime_experiment)
    if regime_manifest.get("status") != "COMPLETE":
        raise ValueError("regime experiment is not complete")
    regimes = pd.read_csv(regime_experiment / "artifacts" / "daily_regimes.csv")
    daily_regime = regimes.set_index(pd.to_datetime(regimes["date"]))["regime"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    frame = intraday.frames[str(dataset["intraday_period"])]
    if frame["Date"].min().date().isoformat() != dataset["start"]:
        raise ValueError("intraday start differs from protocol")
    if frame["Date"].max().date().isoformat() != dataset["cutoff"]:
        raise ValueError("intraday cutoff differs from protocol")

    result = generate_intraday_signal_census(
        frame,
        str(target["symbol"]),
        daily_regime,
        evaluation_start=str(dataset["evaluation_start"]),
        warmup_bars=int(registry["warmup_bars"]),
        window_sessions=int(screen["window_sessions"]),
        median_event_days_required=int(screen["median_event_days_required"]),
        p10_event_days_required=int(screen["p10_event_days_required"]),
        distinct_event_days_required=int(screen["distinct_event_days_required"]),
    )
    result.catalog.to_csv(
        artifacts / "signal_catalog.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    result.states.to_csv(
        artifacts / "state_density.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
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
        "data_fingerprint": _data_fingerprint(intraday.hashes, regime_manifest),
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
            "intraday_manifest": intraday.manifest,
            "intraday_hashes": dict(sorted(intraday.hashes.items())),
            "regime_experiment_manifest": regime_manifest,
            "failures": list(result.failures),
        },
    )

    generated = int(result.summary["generated_configurations"])
    failed = int(result.summary["failed_configurations"])
    states = int(result.summary["observed_primary_states"])
    capable = int(result.summary["density_capable_states"])
    unique = int(result.summary["unique_event_behaviors"])
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        f"状态：COMPLETE。成功运行{generated}个15分钟原生默认信号，失败{failed}个；"
        f"观察到{states}个主状态，其中{capable}个满足30交易日事件密度必要条件。"
        "全部状态事件已按交易日去重，并与冻结的开盘前regime对齐。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n"
        f"15分钟CZSC目录共成功生成{generated}个配置、{states}个主状态，形成"
        f"{unique}种不同的按日事件行为；{capable}个状态通过预注册的事件密度必要条件。"
        "这些状态只取得进入机制研究的资格，尚无买卖方向、收益优势、T+1可执行性或"
        "候选资格。\n\n"
        "下一轮从通过密度筛选且金融语义可解释的非冗余状态中形成小规模机制假设，"
        "预先冻结方向和退出区间，再用下一根5分钟开盘价与T+1约束做因果事件研究。"
        "日线regime只用于分层和有/无消融，不作为强制交易开关。\n",
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
