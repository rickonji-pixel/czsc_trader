"""Run the frozen pre-2026 588080 champion-challenger experiment."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import shutil
import subprocess
from zoneinfo import ZoneInfo

import pandas as pd

from czsc_trader.attribution_runner import run_champion_attribution
from czsc_trader.baselines import resolve_baseline
from czsc_trader.ex04_attribution_runner import (
    run_ex04_attribution,
    validate_protocol as validate_ex04_attribution_protocol,
)
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    create_experiment_dir,
    validate_experiment_archive,
)
from czsc_trader.experiments import run_pre2026_experiment


DEFAULT_PROTOCOL = Path("experiments/0824_EX01/artifacts/protocol.json")


def _installed_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not-installed"


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, encoding="utf-8"
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _validate_attribution_protocol(protocol: dict[str, object]) -> None:
    if protocol.get("experiment_type") != "champion_attribution":
        raise ValueError("experiment directory is not a champion attribution protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("attribution protocol must retain PRE_REGISTERED status")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("attribution protocol must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("attribution protocol must forbid holdout access")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, dict) or any(bool(value) for value in promotion.values()):
        raise ValueError("attribution protocol must disable every promotion action")


def _classification_markdown(artifacts_dir: Path) -> str:
    path = artifacts_dir / "classification.csv"
    if not path.is_file():
        return "机器分类文件缺失，不能形成归因结论。"
    frame = pd.read_csv(path)
    if frame.empty:
        return "没有可分类的归因对象。"
    lines: list[str] = []
    ordered = (
        "stable_negative",
        "stable_positive",
        "return_positive_risk_negative",
        "risk_positive_return_negative",
        "regime_dependent",
        "interaction",
        "redundant",
        "inconclusive",
        "insufficient_sample",
        "unmodeled_state",
    )
    labels = {
        "stable_negative": "稳定负向",
        "stable_positive": "稳定正向",
        "return_positive_risk_negative": "收益正向、风险负向",
        "risk_positive_return_negative": "风险正向、收益负向",
        "regime_dependent": "状态依赖",
        "interaction": "交互型",
        "redundant": "冗余",
        "inconclusive": "证据不足",
        "insufficient_sample": "样本不足",
        "unmodeled_state": "未建模状态",
    }
    for classification in ordered:
        subset = frame.loc[frame["classification"] == classification]
        if subset.empty:
            continue
        lines.append(f"### {labels[classification]}（{len(subset)}项）")
        lines.append("")
        if classification in {"stable_negative", "stable_positive", "regime_dependent", "interaction"}:
            lines.append("| 类型 | 对象 | 反事实 | 收益中位增量 | 夏普中位增量 |")
            lines.append("|---|---|---|---:|---:|")
            for _, row in subset.iterrows():
                lines.append(
                    f"| {row['object_type']} | `{row['object_id']}` | "
                    f"`{row['counterfactual']}` | {float(row['median_return_delta']):+.4%} | "
                    f"{float(row['median_sharpe_delta']):+.4f} |"
                )
        else:
            lines.append("、".join(f"`{value}`" for value in subset["object_id"].astype(str)))
        lines.append("")
    return "\n".join(lines).rstrip()


def _sensitivity_markdown(artifacts_dir: Path) -> str:
    lines: list[str] = []
    for filename, label in (
        ("weight_sensitivity.csv", "权重"),
        ("threshold_sensitivity.csv", "阈值"),
        ("state_machine_sensitivity.csv", "状态机"),
    ):
        path = artifacts_dir / filename
        if not path.is_file():
            continue
        frame = pd.read_csv(path)
        if frame.empty or "local_classification" not in frame:
            continue
        candidates = frame.loc[
            frame["local_classification"] == "stable_local_improvement",
            ["object_id", "counterfactual"],
        ].drop_duplicates()
        if candidates.empty:
            lines.append(f"- {label}：未发现稳定局部改善方向。")
        else:
            values = "、".join(
                f"`{row.object_id} / {row.counterfactual}`"
                for row in candidates.itertuples(index=False)
            )
            lines.append(f"- {label}：{values}。")
    return "\n".join(lines) if lines else "- 没有可用的局部敏感性结果。"


def run_preregistered_attribution(experiment_dir: Path) -> Path:
    """Finalize one existing PRE_REGISTERED attribution archive in place."""
    experiment_dir = Path(experiment_dir).resolve()
    protocol_path = experiment_dir / "artifacts" / "protocol.json"
    if not protocol_path.is_file():
        raise FileNotFoundError(f"missing preregistered protocol: {protocol_path}")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    _validate_attribution_protocol(protocol)
    artifacts_dir = protocol_path.parent
    champion = protocol.get("champion")
    if not isinstance(champion, dict):
        raise ValueError("attribution protocol is missing champion identity")
    try:
        summary = run_champion_attribution(
            Path("data/raw"), Path("configs/rule_baselines"), artifacts_dir, protocol
        )
        if summary.get("status") != "COMPLETE":
            raise ValueError("attribution runner did not complete")
        if summary.get("holdout_accessed") is not False:
            raise ValueError("attribution runner reported holdout access")
        if summary.get("frozen_challenger") is not None:
            raise ValueError("diagnostic attribution produced a frozen challenger")
        hashes = summary.get("visible_data_hashes", {})
        if not isinstance(hashes, dict) or any("2026" in str(name) for name in hashes):
            raise ValueError("attribution result contains a 2026 data hash")

        executed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        (experiment_dir / "03_execution.md").write_text(
            "# 执行过程\n\n"
            "## 正式执行\n\n"
            f"- 状态：`{summary['status']}`\n"
            f"- 执行时间：{executed_at}\n"
            f"- 代码提交：`{_git_head()}`\n"
            f"- Python：{platform.python_version()}\n"
            f"- CZSC：{_installed_version('czsc')}\n"
            f"- vectorbt：{_installed_version('vectorbt')}\n"
            f"- pandas：{_installed_version('pandas')}\n"
            f"- NumPy：{_installed_version('numpy')}\n"
            f"- Plotly：{_installed_version('plotly')}\n"
            f"- 可见行情文件数：{len(hashes)}\n"
            f"- 实际回测目标仓位数：{summary.get('evaluated_target_count', '见metrics.json')}\n"
            "- 2026样本外数据访问：否\n"
            "- 冻结挑战者：未生成\n"
            "- 机器证据：`artifacts/`\n",
            encoding="utf-8",
        )
        (experiment_dir / "04_conclusion.md").write_text(
            "# 研究结论\n\n"
            "## 判定边界\n\n"
            "本轮是诊断实验，不判定策略PASS/FAIL，不更新冠军，也不产生挑战者。"
            "以下结论只描述当前冠军在2021—2025可见样本中的反事实归因。\n\n"
            "## 因素分类\n\n"
            f"{_classification_markdown(artifacts_dir)}\n\n"
            "## 局部敏感性\n\n"
            f"{_sensitivity_markdown(artifacts_dir)}\n\n"
            "## 后续边界\n\n"
            "只有归类为稳定负向的对象可以进入下一轮优化候选清单；"
            "证据不足、状态依赖或单一区间主导的对象不得直接优化。"
            "2026保持不可见，冠军继续为 `baseline_20260823`。\n",
            encoding="utf-8",
        )
        status = "COMPLETE"
    except Exception as exc:
        (experiment_dir / "03_execution.md").write_text(
            "# 执行过程\n\n"
            "- 状态：`ERROR`\n"
            f"- 异常：{type(exc).__name__}: {exc}\n"
            "- 2026样本外数据访问：否\n",
            encoding="utf-8",
        )
        (experiment_dir / "04_conclusion.md").write_text(
            "# 研究结论\n\n正式执行失败，没有归因结论，不得优化冠军或访问2026。\n",
            encoding="utf-8",
        )
        status = "ERROR"
        build_experiment_manifest(
            experiment_dir,
            {
                "experiment_id": experiment_dir.name,
                "date": "2026-08-24",
                "status": status,
                "symbol": protocol.get("symbol", "588080.SH"),
                "asset_type": "etf",
                "champion": champion,
                "visible_sample_end": "2025-12-31",
                "holdout_accessed": False,
            },
        )
        validate_experiment_archive(experiment_dir)
        raise

    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-08-24",
            "status": status,
            "symbol": protocol.get("symbol", "588080.SH"),
            "asset_type": "etf",
            "champion": champion,
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return experiment_dir


def _ex04_conclusion_markdown(artifacts_dir: Path) -> str:
    classifications = pd.read_csv(artifacts_dir / "classification.csv")
    local = json.loads((artifacts_dir / "local_geometry.json").read_text(encoding="utf-8"))
    bootstrap = json.loads(
        (artifacts_dir / "bootstrap_summary.json").read_text(encoding="utf-8")
    )
    metrics = json.loads((artifacts_dir / "metrics.json").read_text(encoding="utf-8"))
    windows = pd.read_csv(artifacts_dir / "window_comparison.csv")
    quantiles = bootstrap["quantiles"]
    lines = [
        "# 研究结论",
        "",
        "## 判定边界",
        "",
        "本轮是EX04机制归因与稳定性诊断，状态为 **COMPLETE**。"
        "不判定策略PASS/FAIL，不生成挑战者，不访问2026。",
        "",
        "## 核心机器结论",
        "",
        f"- 局部几何：`{local['classification']}`。",
        f"- 单一区间主导：`{str(bool(metrics['single_interval_dominated'])).lower()}`。",
        f"- 配对区块bootstrap：{int(bootstrap['replications'])}次；"
        f"2.5%/中位数/97.5%分位为"
        f"{float(quantiles['0.025']):+.4%} / {float(quantiles['0.5']):+.4%} / "
        f"{float(quantiles['0.975']):+.4%}；差值大于0的比例"
        f"{float(bootstrap['probability_delta_above_zero']):.2%}。",
        "",
        "## 稳定机制分类",
        "",
        "| 类型 | 对象 | 分类 | 正向年度 | 负向年度 | 收益贡献中位数 | 夏普贡献中位数 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in classifications.itertuples(index=False):
        lines.append(
            f"| {row.object_type} | `{row.object_id}` | `{row.classification}` | "
            f"{int(row.positive_years)} | {int(row.negative_years)} | "
            f"{float(row.median_return_contribution):+.4%} | "
            f"{float(row.median_sharpe_contribution):+.4f} |"
        )
    lines.extend(
        [
            "",
            "## 年度EX04相对通用基线",
            "",
            "| 年度 | 收益增量 | 夏普增量 | 基线持仓率 | EX04持仓率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    annual = windows.loc[windows["window"].astype(str).isin([str(year) for year in range(2021, 2026)])]
    for row in annual.itertuples(index=False):
        lines.append(
            f"| {row.window} | {float(row.return_delta):+.4%} | "
            f"{float(row.sharpe_delta):+.4f} | {float(row.baseline_exposure):.2%} | "
            f"{float(row.ex04_exposure):.2%} |"
        )
    lines.extend(
        [
            "",
            "## 后续边界",
            "",
            "后续方向只能按预注册映射从上述分类、局部几何、路径集中度和bootstrap证据推导。"
            "混合或被单一区间主导的证据不得强行转化为优化对象；本轮结果不得反馈修改本轮参数。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_preregistered_ex04_attribution(experiment_dir: Path) -> Path:
    """Finalize the preregistered EX04 diagnosis in its existing archive."""
    experiment_dir = Path(experiment_dir).resolve()
    protocol_path = experiment_dir / "artifacts" / "protocol.json"
    if not protocol_path.is_file():
        raise FileNotFoundError(f"missing preregistered protocol: {protocol_path}")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    validate_ex04_attribution_protocol(protocol)
    research_object = protocol.get("research_object")
    if not isinstance(research_object, dict):
        raise ValueError("EX04 attribution protocol is missing research object identity")
    status = "ERROR"
    try:
        summary = run_ex04_attribution(
            Path("data/raw"), Path("configs/rule_baselines"), experiment_dir, protocol
        )
        if summary.get("status") != "COMPLETE":
            raise ValueError("EX04 attribution runner did not complete")
        if summary.get("holdout_accessed") is not False:
            raise ValueError("EX04 attribution runner reported holdout access")
        if summary.get("frozen_challenger") is not None:
            raise ValueError("EX04 diagnosis produced a frozen challenger")
        hashes = summary.get("visible_data_hashes", {})
        if not isinstance(hashes, dict) or any("2026" in str(name) for name in hashes):
            raise ValueError("EX04 diagnosis contains a 2026 data hash")
        if (experiment_dir / "artifacts" / "frozen_challenger.json").exists():
            raise ValueError("EX04 diagnosis wrote a forbidden frozen challenger")
        executed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        (experiment_dir / "03_execution.md").write_text(
            "# 执行过程\n\n"
            "## 正式执行\n\n"
            "- 状态：`COMPLETE`\n"
            f"- 执行时间：{executed_at}\n"
            f"- 执行提交：`{_git_head()}`\n"
            f"- Python：{platform.python_version()}\n"
            f"- CZSC：{_installed_version('czsc')}\n"
            f"- vectorbt：{_installed_version('vectorbt')}\n"
            f"- pandas：{_installed_version('pandas')}\n"
            f"- NumPy：{_installed_version('numpy')}\n"
            f"- 可见行情文件数：{len(hashes)}\n"
            f"- 组件组合：{summary['component_variant_count']}项\n"
            f"- 因子组联盟：{summary['group_coalition_count']}项\n"
            f"- 局部扰动：{summary['local_perturbation_count']}项\n"
            "- 2026数据访问：否\n"
            "- 冻结挑战者：未生成\n"
            "- 机器证据：`artifacts/`\n",
            encoding="utf-8",
        )
        (experiment_dir / "04_conclusion.md").write_text(
            _ex04_conclusion_markdown(experiment_dir / "artifacts"), encoding="utf-8"
        )
        status = "COMPLETE"
    except Exception as exc:
        (experiment_dir / "03_execution.md").write_text(
            "# 执行过程\n\n"
            "- 状态：`ERROR`\n"
            f"- 异常：{type(exc).__name__}: {exc}\n"
            "- 2026数据访问：否\n",
            encoding="utf-8",
        )
        (experiment_dir / "04_conclusion.md").write_text(
            "# 研究结论\n\n正式执行失败，没有归因结论，不得优化EX04或访问2026。\n",
            encoding="utf-8",
        )
        build_experiment_manifest(
            experiment_dir,
            {
                "experiment_id": experiment_dir.name,
                "date": "2026-08-25",
                "status": status,
                "symbol": protocol.get("symbol", "588080.SH"),
                "asset_type": "etf",
                "research_object": research_object,
                "visible_sample_end": "2025-12-31",
                "holdout_accessed": False,
            },
        )
        validate_experiment_archive(experiment_dir)
        raise
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-08-25",
            "status": status,
            "symbol": protocol.get("symbol", "588080.SH"),
            "asset_type": "etf",
            "research_object": research_object,
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return experiment_dir


def main(
    experiments_root: Path = Path("experiments"),
    *,
    run_date: date | None = None,
    protocol_path: Path = DEFAULT_PROTOCOL,
) -> Path:
    """Run the pre-2026 search and finalize one tracked research archive."""
    effective_date = run_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    protocol_path = Path(protocol_path)
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise SystemExit("experiment protocol must stop before 2026")
    experiment_dir = create_experiment_dir(Path(experiments_root), effective_date)
    artifacts_dir = experiment_dir / "artifacts"
    artifacts_dir.mkdir()
    shutil.copyfile(protocol_path, artifacts_dir / "protocol.json")
    baseline = resolve_baseline(Path("configs/rule_baselines"), protocol["champion"])
    try:
        summary = run_pre2026_experiment(
            Path("data/raw"), Path("configs/rule_baselines"), artifacts_dir
        )
    except Exception as exc:
        (experiment_dir / "01_goal.md").write_text(
            "# 研究目标\n\n本轮目标见 `artifacts/protocol.json`；执行未完成。\n",
            encoding="utf-8",
        )
        (experiment_dir / "02_design.md").write_text(
            "# 研究设计\n\n预注册设计见 `artifacts/protocol.json`；执行未完成。\n",
            encoding="utf-8",
        )
        (experiment_dir / "03_execution.md").write_text(
            "# 执行过程\n\n"
            f"- 日期：{effective_date.isoformat()}\n"
            "- 状态：ERROR\n"
            f"- 异常：{type(exc).__name__}: {exc}\n"
            "- 样本外数据访问：否\n",
            encoding="utf-8",
        )
        (experiment_dir / "04_conclusion.md").write_text(
            "# 研究结论\n\n执行失败，未形成研究结论，不得晋升挑战者或访问样本外数据。\n",
            encoding="utf-8",
        )
        build_experiment_manifest(
            experiment_dir,
            {
                "experiment_id": experiment_dir.name,
                "date": effective_date.isoformat(),
                "status": "ERROR",
                "symbol": protocol.get("symbol", "588080.SH"),
                "asset_type": "etf",
                "champion": {"version": baseline.version, "sha256": baseline.sha256},
                "visible_sample_end": protocol["visible_sample_end"],
                "holdout_accessed": False,
            },
        )
        validate_experiment_archive(experiment_dir)
        raise
    metrics = json.loads((artifacts_dir / "metrics.json").read_text(encoding="utf-8"))
    (experiment_dir / "01_goal.md").write_text(
        "# 研究目标\n\n"
        f"- 标的：{protocol.get('symbol', '588080.SH')}\n"
        f"- 冠军：{protocol['champion']}\n"
        f"- 可见样本截止：{protocol['visible_sample_end']}\n"
        f"- PASS：{protocol['pass_rule']}\n",
        encoding="utf-8",
    )
    (experiment_dir / "02_design.md").write_text(
        "# 研究设计\n\n"
        f"- 单一变化：{protocol.get('single_change', 'post-exit re-entry constraint')}\n"
        f"- 候选数量：{len(protocol.get('cooldown_days', [])) * len(protocol.get('reentry_gates', [])) or '见协议'}\n"
        "- 完整预注册协议：`artifacts/protocol.json`\n"
        "- 2026数据在候选冻结前不可见。\n",
        encoding="utf-8",
    )
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 日期：{effective_date.isoformat()}\n"
        f"- 状态：{summary['status']}\n"
        f"- 最佳候选：{summary['best_candidate']}\n"
        f"- 可见行情文件数：{len(metrics.get('visible_data_hashes', {}))}\n"
        "- 样本外数据访问：否\n"
        "- 机器证据：`artifacts/`\n",
        encoding="utf-8",
    )
    frozen = summary.get("frozen_challenger")
    (experiment_dir / "04_conclusion.md").write_text(
        "# 研究结论\n\n"
        f"- 整体结果：{summary['status']}\n"
        f"- 最佳候选：{summary['best_candidate']}\n"
        f"- 通过窗口数：{summary['best_pass_count']}\n"
        f"- 冻结挑战者：{'是' if frozen else '否'}\n"
        "- 详细候选与逐窗口指标见 `artifacts/`。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": effective_date.isoformat(),
            "status": summary["status"],
            "symbol": protocol.get("symbol", "588080.SH"),
            "asset_type": "etf",
            "champion": {"version": baseline.version, "sha256": baseline.sha256},
            "visible_sample_end": protocol["visible_sample_end"],
            "holdout_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return experiment_dir


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    args = parser.parse_args()
    if args.experiment_dir is not None:
        protocol_path = args.experiment_dir / "artifacts" / "protocol.json"
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        experiment_type = protocol.get("experiment_type")
        if experiment_type == "champion_attribution":
            completed = run_preregistered_attribution(args.experiment_dir)
        elif experiment_type == "ex04_mechanism_attribution":
            completed = run_preregistered_ex04_attribution(args.experiment_dir)
        else:
            raise ValueError(f"unsupported preregistered experiment type: {experiment_type}")
        print(completed)
    else:
        main(protocol_path=args.protocol)


if __name__ == "__main__":
    cli()
