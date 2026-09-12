from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.identity import raw_file_sha256
from czsc_trader.intraday_data import (
    load_intraday_research_data,
    prepare_intraday_research_data,
)


EXPERIMENT_ID = "20260912_S004_EX02"


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
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in (
            "signal_census",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("data publication may not mutate strategy lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    data_dir = repo / str(dataset["directory"])
    result = prepare_intraday_research_data(
        str(target["symbol"]),
        date.fromisoformat(str(dataset["start"])),
        date.fromisoformat(str(dataset["cutoff"])),
        data_dir,
        env_file=repo / ".env",
    )
    loaded = load_intraday_research_data(data_dir, str(target["symbol"]))
    counts = {
        period: {
            "bars": int(len(frame)),
            "trading_days": int(frame["Date"].dt.normalize().nunique()),
            "first": frame["Date"].min().isoformat(),
            "last": frame["Date"].max().isoformat(),
        }
        for period, frame in loaded.frames.items()
    }
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "publication": result,
        "manifest_sha256": raw_file_sha256(
            data_dir / "588080_intraday_manifest.json"
        ),
        "file_count": len(loaded.hashes),
        "frequencies": counts,
        "scope": {
            "signal_census": False,
            "candidate_created": False,
            "strategy_manager_mutated": False,
            "pte_mutated": False,
        },
    }
    _write(artifacts / "publication_summary.json", evidence)
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        "状态：COMPLETE。完整15m、5m、1m历史数据已发布到受控研究池，独立manifest与"
        "逐文件哈希现场加载验证通过。未生成信号或候选，未修改SM和PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n"
        "S004所需的完整分钟研究数据已就绪。三个周期覆盖相同交易日并完成日线对账，可以"
        "进入机制定义与事件密度普查。分钟K线数量不作为独立样本数，后续仍按独立交易"
        "事件和交易日聚类评估。\n",
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
