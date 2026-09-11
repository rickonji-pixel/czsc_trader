from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.cross_asset_daily_data import (
    DailySnapshot,
    align_adjusted_to_target_calendar,
    canonicalize_daily,
)
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)


EXPERIMENT_ID = "20260911_S003_EX21"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_sources(repo_root: Path, protocol: dict[str, object]) -> dict[str, DailySnapshot]:
    source = protocol["source_evidence"]
    ex20 = repo_root / "experiments" / source["experiment_id"]
    validate_experiment_archive(ex20)
    expected = {
        ex20 / "experiment_manifest.json": source["experiment_manifest_sha256"],
        ex20 / "artifacts" / "source_evidence.json": source["source_evidence_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"EX20 evidence hash differs: {path.name}")
    snapshots = {}
    for symbol in ("510500.SH", "510300.SH", "512100.SH"):
        code = symbol.split(".")[0]
        artifacts = ex20 / "artifacts"
        snapshots[symbol] = DailySnapshot(
            symbol,
            canonicalize_daily(pd.read_csv(artifacts / f"{code}_adjusted_daily.csv"), symbol),
            canonicalize_daily(pd.read_csv(artifacts / f"{code}_execution_daily.csv"), symbol),
            {"source_experiment": source["experiment_id"]},
        )
    return snapshots


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "conditional_return_analysis",
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX21 may only align and validate data evidence")

    snapshots = _load_sources(repo_root, protocol)
    allowed = {
        symbol: set(spec["dates"])
        for symbol, spec in protocol["alignment"]["allowed_absences"].items()
    }
    panel, audit = align_adjusted_to_target_calendar(
        snapshots,
        protocol["alignment"]["calendar_owner"],
        allowed,
    )
    output = panel.copy()
    output["dt"] = pd.to_datetime(output["dt"]).dt.strftime("%Y-%m-%d")
    output["source_dt"] = pd.to_datetime(output["source_dt"]).dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "aligned_adjusted_daily.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    _write_json(artifacts / "alignment_audit.json", audit)

    carried = audit["symbols"]["512100.SH"]
    (experiment / "03_execution.md").write_text(
        "# S003 EX21 执行\n\n"
        f"以510500的{audit['target_sessions']}个交易日为主日历完成三标的对齐，范围为"
        f"{audit['first_session']}至{audit['last_session']}。512100共延续"
        f"{carried['carried_sessions']}个合法停牌日，日期为{carried['carried_dates']}，最大陈旧度"
        f"为{carried['maximum_staleness_sessions']}个交易日。其余标的没有延续记录。\n\n"
        "所有延续值均来自当时已发布的最近收盘价，没有使用复牌后的未来价格。本轮没有计算"
        "条件未来收益或生成信号。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX21 结论\n\n"
        "结论：`PASS`。三标的因果日线面板已建立；合法停牌被显式标注，未来数据没有回填。"
        "该面板可以进入下一轮相对风格事件定义与密度普查。\n\n"
        "该结论只证明数据对齐规则有效，不构成任何策略收益或稳健性证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": protocol["dataset"]["development_cutoff"],
            "status": "PASS",
            "target_sessions": audit["target_sessions"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
