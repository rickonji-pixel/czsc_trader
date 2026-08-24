"""Run the preregistered EX08 pure-memory 4096-Trial formal study."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from czsc_trader.ex08_runner import run_ex08_experiment


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo_root, text=True
    ).strip()
    if status:
        raise RuntimeError("formal EX08 must start from a clean committed tree")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
    ).strip()
    result = run_ex08_experiment(
        repo_root / "data" / "raw",
        repo_root / "configs" / "rule_baselines",
        repo_root / "experiments" / "0824_EX08",
        execution_commit=commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
