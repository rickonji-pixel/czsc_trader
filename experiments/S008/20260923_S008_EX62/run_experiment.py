from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd
from dotenv import load_dotenv
from strategy_runtime import StrategyCandidate, StrategyInit, StrategyRuntime, TradableWindow
from strategy_runtime.implementation_identity import implementation_sha256
from strategy_runtime.models import CutoffRule

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX62"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_reset(path: Path, parent: Path) -> None:
    target = path.resolve()
    root = parent.resolve()
    if target.parent != root or target.name != "S008_EX62_prepared":
        raise ValueError("unsafe EX62 temporary path")
    if target.exists():
        shutil.rmtree(target)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from protocol")
    predecessor = repo / "experiments/S008/20260923_S008_EX61"
    runtime_root = experiment / "runtime/strategy_runtime"
    runtime_path = runtime_root / "strategies/s008_precious_metal_preference.py"
    if _sha256(predecessor / "experiment_manifest.json") != protocol["sources"]["ex61_manifest_sha256"]:
        raise ValueError("EX61 manifest differs from frozen source")
    if _sha256(runtime_path) != protocol["sources"]["runtime_file_sha256"]:
        raise ValueError("EX62 runtime file differs from frozen source")
    validate_experiment_archive(predecessor)
    runtime_identity = implementation_sha256(
        ("strategies/s008_precious_metal_preference.py",), source_root=runtime_root
    )
    if runtime_identity != protocol["sources"]["runtime_source_sha256"]:
        raise ValueError("EX62 runtime closure differs from frozen source")
    if any(
        protocol.get(key)
        for key in ("reads_real_returns", "starts_search", "selects_parameters", "candidate_generation")
    ):
        raise ValueError("input compatibility gate cannot search, select or promote")

    candidate = StrategyCandidate(
        "S008",
        "EX62INPUT",
        {
            "runtime": {
                "module": "strategy_runtime.strategies.s008_precious_metal_preference",
                "qualname": "S008PreciousMetalPreference",
                "contract_version": 1,
                "source_files": ["strategies/s008_precious_metal_preference.py"],
                "source_sha256": runtime_identity,
            },
            "parameters": {
                "prototype_id": protocol["prototype_id"],
                **dict(protocol["anchor_parameters"]),
            },
        },
        runtime_root,
    )
    definition = StrategyRuntime().describe(candidate)
    requirements = {item.name: item for item in definition.inputs.requirements}
    for name in protocol["input_correction"]["inputs"]:
        requirement = requirements[name]
        if (
            requirement.cutoff_rule is not CutoffRule.LATEST_AVAILABLE
            or requirement.maximum_staleness_days != 7
        ):
            raise ValueError(f"cross-market input contract differs: {name}")

    load_dotenv(repo / str(protocol["dotenv_path"]), override=False)
    tmp_parent = repo / ".tmp"
    tmp_parent.mkdir(exist_ok=True)
    prepared_root = tmp_parent / "S008_EX62_prepared"
    _safe_reset(prepared_root, tmp_parent)
    instance = StrategyRuntime().create(
        StrategyInit(
            candidate,
            TradableWindow(
                pd.Timestamp(protocol["evaluation_start"]).date(),
                pd.Timestamp(protocol["development_cutoff"]).date(),
            ),
            prepared_root,
        )
    )
    prepared = instance.prepare_data()
    history = instance.inspect_signals()
    xau_age = (history.index - pd.to_datetime(history["xau_source_date"])).days
    xag_age = (history.index - pd.to_datetime(history["xag_source_date"])).days
    if (xau_age < 1).any() or (xag_age < 1).any():
        raise ValueError("cross-market source date is not strictly prior")
    if (xau_age > 7).any() or (xag_age > 7).any():
        raise ValueError("cross-market source date exceeds seven-day staleness")
    targets = set(pd.to_numeric(history["target_position"], errors="raise"))
    entries = int(history["action"].eq("ENTER").sum())
    exits = int(history["action"].eq("EXIT").sum())
    if targets != {0.0, 1.0} or entries < 1 or exits < 1:
        raise ValueError("real development data does not exercise both anchor states")

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "PROCEED_TO_PRECIOUS_METAL_FORMAL_SEARCH_SUCCESSOR",
        "runtime_source_sha256": runtime_identity,
        "prepared_data_identity": prepared.data_identity,
        "history_rows": len(history),
        "xau_max_staleness_days": int(xau_age.max()),
        "xag_max_staleness_days": int(xag_age.max()),
        "strict_prior_source_dates": True,
        "target_states": [0.0, 1.0],
        "entries": entries,
        "exits": exits,
        "real_returns_read": False,
        "search_started": False,
        "parameters_selected": False,
        "candidate_created": False,
        "sealed_validation_read": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "input_compatibility_evidence.json", evidence)
    _safe_reset(prepared_root, tmp_parent)
    (experiment / "03_execution.md").write_text(
        "# S008 EX62 执行记录\n\n"
        f"完整开发窗口DFLS准备成功，共{len(history)}个信号日；XAU/XAG最大陈旧度分别为"
        f"{evidence['xau_max_staleness_days']}日和{evidence['xag_max_staleness_days']}日，全部源日期"
        f"严格早于中国信号日。锚点产生{entries}次入场、{exits}次退出。未启动参数搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX62 结论\n\n"
        "机器裁决：`PROCEED_TO_PRECIOUS_METAL_FORMAL_SEARCH_SUCCESSOR`。跨市场输入合同已在真实"
        "开发窗口闭合，修正不改变特征滞后和严格因果边界。下一步可建立正式搜索技术后继。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
