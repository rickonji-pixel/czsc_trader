"""Run the engineering-only EX07 in-memory Optuna throughput benchmark."""

from __future__ import annotations

import argparse
from pathlib import Path

from czsc_trader.optuna_benchmark import run_inmemory_benchmark


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repo_root / "outputs" / "benchmarks",
    )
    args = parser.parse_args()
    result = run_inmemory_benchmark(
        repo_root / "data" / "raw",
        repo_root / "configs" / "rule_baselines",
        repo_root / "experiments" / "0824_EX07",
        args.output_root,
        completed_trials=args.trials,
    )
    print(f"benchmark_path={result['benchmark_path']}")
    print(f"trials_per_hour={float(result['trials_per_hour']):.2f}")


if __name__ == "__main__":
    main()
