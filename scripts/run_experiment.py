"""Run the frozen pre-2026 588080 champion-challenger experiment."""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

from czsc_trader.baselines import resolve_baseline
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    create_experiment_dir,
    validate_experiment_archive,
)
from czsc_trader.experiments import run_pre2026_experiment


DEFAULT_PROTOCOL = Path("experiments/0824_EX01/artifacts/protocol.json")


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


if __name__ == "__main__":
    main()
