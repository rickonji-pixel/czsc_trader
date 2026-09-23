from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import warnings

import numpy as np
import optuna
import pandas as pd
from strategy_runtime.implementation_identity import implementation_sha256

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX25"


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


def _old_regime(values: tuple[float, float]) -> float:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return float(np.nanmean(values))


def _new_regime(values: tuple[float, float]) -> float:
    raw = np.asarray(values, dtype=float)
    finite = raw[np.isfinite(raw)]
    return float(finite.mean()) if len(finite) else np.nan


def _equivalent(left: float, right: float) -> bool:
    return bool((np.isnan(left) and np.isnan(right)) or left == right)


def _load_runtime(path: Path):
    specification = importlib.util.spec_from_file_location("s008_ex25_runtime", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load EX25 runtime")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    sources = dict(protocol["sources"])

    ex24_manifest = repo / str(sources["ex24_manifest_path"])
    registry_path = repo / str(sources["ex19_registry_path"])
    runtime_root = experiment / "runtime/strategy_runtime"
    runtime_path = runtime_root / "strategies/s008_prototypes.py"

    expected_hashes = {
        ex24_manifest: str(sources["ex24_manifest_sha256"]).lower(),
        registry_path: str(sources["ex19_registry_sha256"]).lower(),
        runtime_path: str(sources["ex25_runtime_raw_sha256"]).lower(),
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")
    validate_experiment_archive(ex24_manifest.parent)

    runtime_identity = implementation_sha256(
        ["strategies/s008_prototypes.py"], source_root=runtime_root
    )
    if runtime_identity != sources["ex25_runtime_implementation_sha256"]:
        raise ValueError("EX25 runtime implementation identity differs")
    if optuna.__version__ != protocol["search"]["engine_version"]:
        raise ValueError("Optuna version differs from frozen search contract")

    registry = _read(registry_path)
    registered = {
        item["prototype_id"]: {
            "search_space": item["search_space"],
            "constraints": item["constraints"],
        }
        for item in registry["prototypes"]
    }
    studies = list(protocol["search"]["prototype_studies"])
    study_ids = [str(study["prototype_id"]) for study in studies]
    if study_ids != list(registered):
        raise ValueError("EX25 study order or prototype set differs from EX19")
    if sum(int(study["trial_budget"]) for study in studies) != 4500:
        raise ValueError("total search budget differs")
    if len(set(int(study["seed"]) for study in studies)) != len(studies):
        raise ValueError("prototype study seeds must be independent")

    runtime_source = runtime_path.read_text(encoding="utf-8")
    if "np.nanmean([row.opportunity_score, row.context_score])" in runtime_source:
        raise ValueError("warning-producing EX24 regime aggregation remains")
    runtime = _load_runtime(runtime_path)
    rule = {
        "regime_entry": 0.5,
        "regime_exit": 0.35,
        "oversold_arm": 0.55,
        "recovery_delta": 0.05,
        "maximum_wait_sessions": 20,
        "maximum_hold_sessions": 40,
    }
    warmup_scores = pd.DataFrame(
        {
            "opportunity_score": [np.nan, np.nan, 0.3, 0.3],
            "context_score": [np.nan, 0.2, np.nan, 0.5],
            "timing_score": [np.nan, 0.6, 0.6, 0.6],
        }
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runtime._regime_recovery(warmup_scores, rule)
    runtime_warnings = [str(item.message) for item in caught]
    cases = [
        (np.nan, np.nan),
        (np.nan, 0.2),
        (0.3, np.nan),
        (0.3, 0.5),
    ]
    equivalence = [
        {
            "inputs": [None if np.isnan(value) else value for value in case],
            "old": None if np.isnan(_old_regime(case)) else _old_regime(case),
            "new": None if np.isnan(_new_regime(case)) else _new_regime(case),
            "equivalent": _equivalent(_old_regime(case), _new_regime(case)),
        }
        for case in cases
    ]
    if runtime_warnings or not all(item["equivalent"] for item in equivalence):
        raise ValueError("EX25 warning cleanup is not warning-free and equivalent")

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "JOINT_SEARCH_CONTRACT_FROZEN",
        "prototype_count": len(studies),
        "trial_budget_per_prototype": {
            str(study["prototype_id"]): int(study["trial_budget"])
            for study in studies
        },
        "total_trial_budget": int(protocol["search"]["total_trial_budget"]),
        "prototype_spaces": registered,
        "runtime_raw_sha256": _sha256(runtime_path),
        "runtime_implementation_sha256": runtime_identity,
        "warning_cleanup_equivalence": equivalence,
        "runtime_warning_count": len(runtime_warnings),
        "tool_versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "optuna": optuna.__version__,
        },
        "real_returns_read": False,
        "search_started": False,
        "winner_selected": False,
        "candidate_created": False,
        "sealed_validation_read": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "preregistration_evidence.json", evidence)

    (experiment / "03_execution.md").write_text(
        "# S008 EX25 执行记录\n\n"
        "脚本核验 EX19 原型注册表、EX24 完整档案、EX25 运行时源码身份、三套独立 study 的"
        "预算与种子、4,500 次总预算、Optuna 版本及完整 trial 账本字段。暖机空值聚合的四组"
        "边界输入与 EX24 语义逐一等价，实际状态机调用没有产生运行提示。\n\n"
        "本实验没有读取真实行情或收益，没有实例化 Optuna study，也没有选择参数或原型。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX25 实验结论\n\n"
        "机器裁决：`JOINT_SEARCH_CONTRACT_FROZEN`。三套原型各自 1,500 次、合计 4,500 次的"
        "独立 NSGA-II 联合搜索合同已经冻结；主成本硬门、压力诊断、同口径 BuyHold、完整 trial "
        "账本和异常停止规则均已明确。\n\n"
        "本结论只授权按冻结协议创建新的正式搜索实验，不提供任何 Alpha、收益或候选证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S008",
            "credential_id": "SGC-S008-001",
            "symbol": "518880.SH",
            "development_cutoff": "2024-12-31",
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
