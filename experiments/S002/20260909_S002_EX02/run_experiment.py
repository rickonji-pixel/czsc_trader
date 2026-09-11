from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import time

import czsc
import czsc._native as czsc_native
import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.signal_census import evaluate_signal_events, generate_signal_census


EXPERIMENT_ID = "20260909_S002_EX02"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def _display(value: float, kind: str) -> str:
    if not np.isfinite(value):
        return "N/A"
    if kind == "percent":
        return f"{value:.2%}"
    return f"{value:.3f}"


def _top_rows(metrics: pd.DataFrame, bias: str) -> pd.DataFrame:
    scoped = metrics.loc[
        (metrics["horizon"] == 5)
        & (metrics["forward_bias"] == bias)
        & (metrics["evidence_quality"] == "OK")
        & ~metrics["state_primary"].isin(["其他", "任意", "中性"])
    ].copy()
    scoped["absolute_effect"] = scoped["standardized_effect"].abs()
    return scoped.sort_values(
        ["year_sign_consistency", "absolute_effect", "event_count"],
        ascending=[False, False, False],
    ).head(10)


def _render_table(rows: pd.DataFrame) -> list[str]:
    lines = [
        "| 频率 | 信号函数 | 主状态 | 事件 | 年份一致性 | 5日净收益 | 5日MFE | 5日MAE |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for _, row in rows.iterrows():
        lines.append(
            f"| {row['frequency']} | `{row['name']}` | {row['state_primary']} | "
            f"{int(row['event_count'])} | {_display(float(row['year_sign_consistency']), 'ratio')} | "
            f"{_display(float(row['net_return_mean']), 'percent')} | "
            f"{_display(float(row['mfe_mean']), 'percent')} | "
            f"{_display(float(row['mae_mean']), 'percent')} |"
        )
    if rows.empty:
        lines.append("| — | — | — | 0 | — | — | — | — |")
    return lines


def _render_conclusion(
    summary: dict[str, object],
    metrics: pd.DataFrame,
    events: pd.DataFrame,
    redundancy: dict[str, object],
) -> str:
    state_count = int(metrics["state_id"].nunique())
    ok_states = int(
        metrics.loc[(metrics["horizon"] == 5) & (metrics["evidence_quality"] == "OK"), "state_id"].nunique()
    )
    sparse_states = int(
        metrics.loc[
            (metrics["horizon"] == 5) & metrics["evidence_quality"].str.contains("SPARSE"),
            "state_id",
        ].nunique()
    )
    positive = _top_rows(metrics, "POSITIVE")
    negative = _top_rows(metrics, "NEGATIVE")
    lines = [
        f"# {EXPERIMENT_ID} 结论",
        "",
        "状态：COMPLETE。510500专属CZSC信号普查与因果事件研究已完成。",
        "",
        "## 普查覆盖",
        "",
        f"CZSC注册表共{summary['registered_total']}个信号；其中{summary['included_functions']}个"
        f"K线信号进入范围，按3个频率形成{summary['requested_configurations']}个默认配置。"
        f"成功生成{summary['generated_configurations']}个，失败或无法映射"
        f"{summary['failed_or_unmapped_configurations']}个。",
        f"正式观察区间共识别{state_count}个主状态、{len(events)}个状态切换事件；按5日"
        f"效果口径，{ok_states}个状态的数据充分性标签为OK，{sparse_states}个状态包含稀疏标签。",
        f"精确行为重复关系共{redundancy['duplicate_group_count']}组，涉及"
        f"{redundancy['duplicate_member_count']}个状态成员。重复只表示日级激活序列完全相同，"
        "不代表信号实现或经济含义相同。",
        "",
        "## 5日正向偏置：下一轮假设复核清单",
        "",
        *_render_table(positive),
        "",
        "## 5日负向偏置：下一轮退出或过滤假设复核清单",
        "",
        *_render_table(negative),
        "",
        "## 结论边界",
        "",
        "这些排序是探索性描述，受信号间高度相关、多重比较和样本路径影响，不构成统计显著性"
        "或可交易性证明。负向偏置表示该状态之后收益较弱，在多头/现金框架中可用于退出或"
        "过滤研究，不等同于可实现的空头收益。",
        "",
        "本轮没有生成S002候选，也没有沿用S001的权重、状态机或regime。下一轮应先人工"
        "检查清单中信号的CZSC语义、重复关系与图形位置，再从少量互补信号提出可解释的入场、"
        "退出和过滤假设；随后才进入组合回测。",
        "",
        "全部配置、状态分布、事件、年度稳定性、重复关系与数据指纹见`artifacts/`。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    started = time.perf_counter()
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read_json(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("candidate_generation", "parameter_search", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("signal census protocol may not create, promote, or deploy a strategy")
    expected_version = str(protocol["signal_universe"]["czsc_version"])
    if str(czsc.__version__) != expected_version:
        raise ValueError(f"CZSC version {czsc.__version__} differs from frozen {expected_version}")

    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    dataset_spec = protocol["dataset"]
    target = protocol["research_target"]
    replay_data = load_replay_data(
        context,
        str(dataset_spec["name"]),
        str(target["symbol"]),
        str(target["asset_type"]),
        date.fromisoformat(str(dataset_spec["cutoff"])),
    )
    registry = [dict(item) for item in czsc_native.list_all_signals()]
    census = generate_signal_census(
        replay_data.adjusted,
        protocol["signal_universe"]["frequencies"],
        evaluation_start=str(dataset_spec["evaluation_start"]),
        registry=registry,
    )
    quality = protocol["evidence_quality"]
    study = protocol["event_study"]
    metrics, stability, events, redundancy = evaluate_signal_events(
        census.primary,
        census.full,
        census.catalog,
        replay_data.execution_daily,
        evaluation_start=str(dataset_spec["evaluation_start"]),
        horizons=[int(value) for value in study["horizons_sessions"]],
        fee_rate=float(study["fee_rate_one_way"]),
        sparse_events_below=int(quality["sparse_events_below"]),
        narrow_coverage_years_below=int(quality["narrow_coverage_years_below"]),
        concentrated_year_share_above=float(quality["concentrated_year_share_above"]),
    )
    if metrics.empty or events.empty:
        raise ValueError("signal census produced no event evidence")

    catalog = census.catalog.sort_values(["status", "frequency", "name"]).reset_index(drop=True)
    metrics = metrics.sort_values(["frequency", "name", "state_primary", "horizon"]).reset_index(drop=True)
    stability = stability.sort_values(["frequency", "name", "state_primary", "horizon", "year"]).reset_index(drop=True)
    events = events.sort_values(["signal_date", "frequency", "name", "state_primary"]).reset_index(drop=True)
    _write_csv(catalog, artifacts / "signal_catalog.csv")
    _write_csv(metrics, artifacts / "event_metrics.csv")
    _write_csv(stability, artifacts / "signal_stability.csv")
    events.to_csv(
        artifacts / "signal_events.csv.gz",
        index=False,
        encoding="utf-8",
        lineterminator="\n",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
    )
    _write_json(artifacts / "redundancy_map.json", redundancy)

    evidence_files = [
        "signal_catalog.csv",
        "event_metrics.csv",
        "signal_stability.csv",
        "signal_events.csv.gz",
        "redundancy_map.json",
    ]
    execution_dates = pd.to_datetime(replay_data.execution_daily["dt"]).dt.normalize()
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "dataset": {
            "name": replay_data.dataset,
            "symbol": replay_data.adjusted.symbol,
            "fingerprint": replay_data.fingerprint,
            "cutoff": replay_data.cutoff.isoformat(),
            "available_first_session": execution_dates.min().date().isoformat(),
            "available_last_session": execution_dates.max().date().isoformat(),
            "evaluation_first_session": execution_dates.loc[execution_dates >= pd.Timestamp(dataset_spec["evaluation_start"])].min().date().isoformat(),
            "market_manifest_sha256": _sha256(context.raw_dir / "510500_manifest.json"),
            "execution_manifest_sha256": _sha256(context.raw_dir / "510500_execution_manifest.json"),
        },
        "czsc": {
            "version": str(czsc.__version__),
            "registry_sha256": hashlib.sha256(_canonical_registry(registry).encode("utf-8")).hexdigest(),
            **census.registry_summary,
        },
        "failures": list(census.failures),
        "outputs": {name: {"bytes": (artifacts / name).stat().st_size, "sha256": _sha256(artifacts / name)} for name in evidence_files},
    }
    _write_json(artifacts / "run_evidence.json", evidence)

    elapsed = time.perf_counter() - started
    (experiment_dir / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        "状态：COMPLETE。\n\n"
        f"按冻结协议完成CZSC注册表盘点、{census.registry_summary['requested_configurations']}个"
        f"默认信号配置的批量生成、状态切换事件研究、逐年稳定性和精确重复审计。"
        f"运行耗时{elapsed:.1f}秒；生成配置{census.registry_summary['generated_configurations']}个，"
        f"失败或无法映射{census.registry_summary['failed_or_unmapped_configurations']}个。\n\n"
        "运行只读取研究数据和CZSC注册表，未生成策略候选，未调用SM或PTE。数据、注册表和"
        "全部结果文件的指纹见`artifacts/run_evidence.json`。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(census.registry_summary, metrics, events, redundancy), encoding="utf-8"
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset_spec["cutoff"],
            "promotion_allowed": False,
        },
    )


def _canonical_registry(registry: list[dict[str, object]]) -> str:
    return json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
