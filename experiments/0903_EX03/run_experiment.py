"""Correct EX02 by searching only the deployable range-weight simplex."""

from __future__ import annotations

from collections.abc import Sequence
import importlib.util
import json
from pathlib import Path
import subprocess
import time

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256, normalized_text_sha256
from czsc_trader.range_platform import (
    dirichlet_weight_candidates,
    robust_pareto_profiles,
    select_robust_seeds,
)


FACTOR_PREFIX = "weight__"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT_DIR / "artifacts"
PROTOCOL_PATH = ARTIFACTS / "protocol.json"
SOURCE_DIR = REPO_ROOT / "experiments" / "0903_EX02"


def deployable_candidates(
    candidates: pd.DataFrame,
    *,
    factor_names: Sequence[str],
    minimum_weight: float,
) -> pd.DataFrame:
    """Keep candidates whose every factor weight satisfies the deployment floor."""
    result = candidates.copy()
    columns = [f"{FACTOR_PREFIX}{name}" for name in factor_names]
    result["minimum_factor_weight"] = result[columns].min(axis=1)
    return result.loc[
        result["minimum_factor_weight"].ge(float(minimum_weight) - 1e-12)
    ].reset_index(drop=True)


def _load_ex02():
    path = SOURCE_DIR / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("range_optimization_source", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load EX02 runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_clean(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _identity_sha(path: Path) -> str:
    return canonical_json_sha256(path) if path.suffix.lower() == ".json" else normalized_text_sha256(path)


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, encoding="utf-8"
    ).strip()


def _protocol() -> dict[str, object]:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    expected = {
        "experiment_id": "0903_EX03",
        "status": "PRE_REGISTERED",
        "minimum_factor_weight": 0.005,
        "source_seed_count": 12,
        "samples_per_seed": 31,
        "new_candidate_count": 372,
        "formal_shortlist_count": 24,
        "core_metrics": ["max_drawdown", "calmar", "win_loss_ratio"],
        "return_used_for_selection": False,
        "automatic_promotion": False,
        "hard_pass_gate": False,
    }
    for key, expected_value in expected.items():
        if payload.get(key) != expected_value:
            raise ValueError(f"protocol field {key} differs from preregistration")
    source_hash = canonical_json_sha256(SOURCE_DIR / "experiment_manifest.json")
    if source_hash != payload["source_experiment"]["manifest_sha256"]:
        raise ValueError("EX02 manifest differs from preregistration")
    return payload


def _source_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = pd.concat(
        [
            pd.read_csv(SOURCE_DIR / "artifacts" / "stage1_candidates.csv"),
            pd.read_csv(SOURCE_DIR / "artifacts" / "stage2_candidates.csv"),
        ],
        ignore_index=True,
    )
    metrics = pd.read_csv(SOURCE_DIR / "artifacts" / "proxy_period_metrics.csv")
    return candidates, metrics


def _new_candidates(
    source: pd.DataFrame,
    seed_ids: Sequence[int],
    factor_names: Sequence[str],
    protocol: dict[str, object],
    context: dict[str, object],
    ex02: object,
) -> pd.DataFrame:
    indexed = source.set_index("candidate_id")
    anchors = {
        f"seed_{candidate_id}": pd.Series(
            {
                name: float(indexed.loc[int(candidate_id), f"{FACTOR_PREFIX}{name}"])
                for name in factor_names
            }
        )
        for candidate_id in seed_ids
    }
    generated = dirichlet_weight_candidates(
        anchors,
        samples_per_anchor=int(protocol["samples_per_seed"]),
        concentrations={name: float(protocol["local_concentration"]) for name in anchors},
        seed=int(protocol["seed"]),
        minimum_weight=float(protocol["minimum_factor_weight"]),
    )
    generated = generated.loc[~generated["is_anchor"]].reset_index(drop=True)
    generated["candidate_id"] = np.arange(
        int(source["candidate_id"].max()) + 1,
        int(source["candidate_id"].max()) + 1 + len(generated),
    )
    generated = generated.rename(columns={name: f"{FACTOR_PREFIX}{name}" for name in factor_names})
    generated.insert(1, "stage", 3)
    generated["parent_seed_id"] = generated["anchor_name"].str.removeprefix("seed_").astype(int)
    generated = ex02._attach_group_shares(generated, context)
    return deployable_candidates(
        generated,
        factor_names=factor_names,
        minimum_weight=float(protocol["minimum_factor_weight"]),
    )


def _write_documents(
    result: dict[str, object], formal_metrics: pd.DataFrame, formal_profiles: pd.DataFrame
) -> None:
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        "# 0903_EX03 执行\n\n"
        f"- 执行提交：`{result['execution_commit']}`。\n"
        f"- EX02可部署源候选：{result['source_deployable_count']}。\n"
        f"- 纠偏局部候选：{result['new_candidate_count']}。\n"
        f"- 正式执行复核：{result['formal_shortlist_count']}项、{result['window_count']}个窗口。\n"
        f"- 全部正式短名单的最小因子权重不低于{result['minimum_factor_weight']:.3%}。\n"
        f"- 总耗时：{result['elapsed_seconds']:.2f}秒。\n",
        encoding="utf-8",
    )
    control_id = int(result["control_candidate_id"])
    challenger_id = int(result["best_challenger_id"])
    selected = formal_metrics.loc[formal_metrics["candidate_id"].isin([control_id, challenger_id])]
    profiles = formal_profiles.set_index("candidate_id")
    lines = [
        "# 0903_EX03 结论",
        "",
        "状态：`COMPLETE`。本轮纠正EX02遗漏的0.5%单因子权重下限；没有硬性PASS/FAIL门槛，不自动冻结或晋升策略。",
        "",
        "## S001-v1与最佳可部署挑战者",
        "",
        "| 窗口 | 方案 | 最大回撤 | 卡玛 | 盈亏比 | 收益率 | 纯range收益 | range→trend收益 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in selected.sort_values(["period", "candidate_id"]).itertuples():
        label = "S001-v1" if int(row.candidate_id) == control_id else f"挑战者{challenger_id}"
        lines.append(
            f"| {row.period} | {label} | {row.max_drawdown:.2%} | {row.calmar:.4f} | "
            f"{row.win_loss_ratio:.4f} | {row.strategy_return:.2%} | "
            f"{row.range_to_range_return:.2%} | {row.range_to_trend_return:.2%} |"
        )
    challenger = profiles.loc[challenger_id]
    control = profiles.loc[control_id]
    lines.extend(
        [
            "",
            "## 稳健性与边界",
            "",
            f"挑战者{challenger_id}在{int(challenger.first_front_count)}个窗口处于三指标Pareto第一前沿，最差层级{int(challenger.worst_pareto_layer)}，平均层级{challenger.mean_pareto_layer:.2f}。",
            f"S001-v1在{int(control.first_front_count)}个窗口处于第一前沿，最差层级{int(control.worst_pareto_layer)}，平均层级{control.mean_pareto_layer:.2f}。",
            f"挑战者最小单因子权重为{result['best_challenger_minimum_weight']:.3%}，满足正式运行下限。",
            "",
            "收益率没有参与排序。所有结果来自截至2026-09-02的开发池，不是新的样本外证据；本实验只提供是否值得冻结S001-v2的研究材料。",
        ]
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> dict[str, object]:
    started = time.perf_counter()
    protocol = _protocol()
    ex02 = _load_ex02()
    ex02_protocol = ex02._protocol()
    context = ex02._load_context(ex02_protocol)
    factor_names = context["factor_names"]
    source_candidates, source_metrics = _source_inputs()
    deployable_source = deployable_candidates(
        source_candidates,
        factor_names=factor_names,
        minimum_weight=float(protocol["minimum_factor_weight"]),
    )
    source_ids = set(deployable_source["candidate_id"].astype(int))
    deployable_metrics, source_profiles = robust_pareto_profiles(
        source_metrics.loc[source_metrics["candidate_id"].isin(source_ids)].drop(
            columns="pareto_layer"
        ),
        protocol["core_metrics"],
        tolerance=float(protocol["comparison_tolerance"]),
    )
    seed_ids = select_robust_seeds(source_profiles, limit=int(protocol["source_seed_count"]))
    candidates = _new_candidates(
        source_candidates, seed_ids, factor_names, protocol, context, ex02
    )
    if len(candidates) != int(protocol["new_candidate_count"]):
        raise AssertionError("corrective candidate count differs")
    new_metrics, _ = ex02._proxy_evaluate(candidates, ex02_protocol, context)
    combined_candidates = pd.concat([deployable_source, candidates], ignore_index=True)
    combined_metrics, combined_profiles = robust_pareto_profiles(
        pd.concat([deployable_metrics.drop(columns="pareto_layer"), new_metrics], ignore_index=True),
        protocol["core_metrics"],
        tolerance=float(protocol["comparison_tolerance"]),
    )
    control_id = int(
        source_candidates.loc[
            source_candidates["anchor_name"].eq("control") & source_candidates["is_anchor"],
            "candidate_id",
        ].iloc[0]
    )
    shortlist_ids = ex02.shortlist_candidate_ids(
        combined_profiles,
        control_id=control_id,
        limit=int(protocol["formal_shortlist_count"]),
    )
    formal_candidates = combined_candidates.loc[
        combined_candidates["candidate_id"].isin(shortlist_ids)
    ].copy()
    targets = {
        int(row.candidate_id): ex02._target_for_weights(
            ex02._weights(pd.Series(row._asdict()), factor_names), context
        )
        for row in formal_candidates.itertuples(index=False)
    }
    formal_metrics, formal_cycles, execution_audit = ex02._formal_evaluate(
        shortlist_ids, formal_candidates, targets, ex02_protocol, context
    )
    formal_metrics, formal_profiles = robust_pareto_profiles(
        formal_metrics,
        protocol["core_metrics"],
        tolerance=float(protocol["comparison_tolerance"]),
    )
    challengers = formal_profiles.loc[formal_profiles["candidate_id"].ne(control_id)]
    best_challenger_id = select_robust_seeds(challengers, limit=1)[0]
    neighborhood = ex02.nearest_neighborhood(
        formal_candidates,
        representative_id=best_challenger_id,
        factor_names=[f"{FACTOR_PREFIX}{name}" for name in factor_names],
        member_count=min(7, len(formal_candidates)),
    )
    best_row = formal_candidates.set_index("candidate_id").loc[best_challenger_id]
    result = {
        "status": "COMPLETE",
        "experiment_id": "0903_EX03",
        "execution_commit": _git_head(),
        "source_experiment": protocol["source_experiment"],
        "strategy_release": ex02_protocol["strategy_release"],
        "development_end": ex02_protocol["development_end"],
        "minimum_factor_weight": float(protocol["minimum_factor_weight"]),
        "source_deployable_count": len(deployable_source),
        "source_seed_ids": seed_ids,
        "new_candidate_count": len(candidates),
        "formal_shortlist_count": len(shortlist_ids),
        "formal_shortlist_ids": shortlist_ids,
        "control_candidate_id": control_id,
        "best_challenger_id": best_challenger_id,
        "best_challenger_minimum_weight": float(best_row["minimum_factor_weight"]),
        "window_count": len(ex02_protocol["development_windows"]),
        "core_metrics": protocol["core_metrics"],
        "return_used_for_selection": False,
        "automatic_promotion": False,
        "execution_audit": execution_audit,
        "elapsed_seconds": time.perf_counter() - started,
    }
    _write_csv(ARTIFACTS / "source_deployable_candidates.csv", deployable_source)
    _write_csv(ARTIFACTS / "source_deployable_profiles.csv", source_profiles)
    _write_csv(ARTIFACTS / "corrective_candidates.csv", candidates)
    _write_csv(ARTIFACTS / "proxy_period_metrics.csv", combined_metrics)
    _write_csv(ARTIFACTS / "proxy_profiles.csv", combined_profiles)
    _write_csv(ARTIFACTS / "formal_shortlist_candidates.csv", formal_candidates)
    _write_csv(ARTIFACTS / "formal_period_metrics.csv", formal_metrics)
    _write_csv(ARTIFACTS / "formal_profiles.csv", formal_profiles)
    _write_csv(ARTIFACTS / "representative_neighborhood.csv", neighborhood)
    _write_csv(ARTIFACTS / "formal_cycles.csv", formal_cycles)
    _write_json(ARTIFACTS / "metrics.json", result)
    _write_documents(result, formal_metrics, formal_profiles)
    build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "schema_version": 1,
            "experiment_id": "0903_EX03",
            "date": "2026-09-03",
            "status": "COMPLETE",
            "symbol": "588080.SH",
            "strategy_release": ex02_protocol["strategy_release"],
            "source_experiment": protocol["source_experiment"],
            "visible_sample_end": ex02_protocol["development_end"],
            "automatic_promotion": False,
        },
    )
    validate_experiment_archive(EXPERIMENT_DIR)
    return result


if __name__ == "__main__":
    print(json.dumps(_clean(run()), ensure_ascii=False))
