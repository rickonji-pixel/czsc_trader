"""Evaluate one tracked frozen challenger on the 2026 holdout."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    create_experiment_dir,
    validate_experiment_archive,
)
from czsc_trader.experiments import run_2026_holdout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-challenger", required=True, type=Path)
    return parser


def main(
    frozen_challenger: Path,
    *,
    experiments_root: Path = Path("experiments"),
    run_date: date | None = None,
) -> Path:
    """Evaluate one frozen challenger in a new tracked research archive."""
    frozen_challenger = Path(frozen_challenger).resolve()
    source_archive = frozen_challenger.parent.parent
    source_manifest = validate_experiment_archive(source_archive)
    if frozen_challenger.parent.name != "artifacts":
        raise ValueError("frozen challenger must be inside an experiment artifacts directory")
    frozen = json.loads(frozen_challenger.read_text(encoding="utf-8"))
    effective_date = run_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    experiment_dir = create_experiment_dir(Path(experiments_root), effective_date)
    artifacts_dir = experiment_dir / "artifacts"
    summary = run_2026_holdout(
        Path("data/raw"),
        Path("configs/rule_baselines"),
        frozen_challenger,
        artifacts_dir,
    )
    windows = summary.get("windows", {})
    window_lines = "\n".join(
        f"- {name}: {'PASS' if values.get('pass') else 'FAIL'}"
        for name, values in windows.items()
    ) or "- 无窗口结果"
    (experiment_dir / "01_goal.md").write_text(
        "# 研究目标\n\n"
        f"对实验 `{source_manifest.get('experiment_id')}` 冻结的挑战者执行一次2026样本外检验。\n",
        encoding="utf-8",
    )
    (experiment_dir / "02_design.md").write_text(
        "# 研究设计\n\n不搜索或调整参数；逐窗口比较挑战者与冠军的收益率和夏普率。\n",
        encoding="utf-8",
    )
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 日期：{effective_date.isoformat()}\n"
        f"- 冻结来源实验：{source_manifest.get('experiment_id')}\n"
        f"- 截止日：{summary.get('holdout_cutoff')}\n"
        "- 样本外数据访问：是\n"
        "- 机器证据：`artifacts/`\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "# 研究结论\n\n"
        f"- 整体结果：{summary['status']}\n"
        f"{window_lines}\n",
        encoding="utf-8",
    )
    champion = frozen.get("champion", summary.get("champion", {}))
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": effective_date.isoformat(),
            "status": summary["status"],
            "symbol": "588080.SH",
            "asset_type": "etf",
            "champion": champion,
            "visible_sample_end": summary.get("holdout_cutoff"),
            "holdout_accessed": True,
            "source_experiment": source_manifest.get("experiment_id"),
        },
    )
    validate_experiment_archive(experiment_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return experiment_dir


if __name__ == "__main__":
    args = _parser().parse_args()
    main(args.frozen_challenger)
