from __future__ import annotations

import hashlib
from pathlib import Path

from czsc_trader.experiment_archive import validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX48"
PREDECESSOR_ID = "20260923_S008_EX47"
PREDECESSOR_MANIFEST_SHA256 = "8665ebe056ffef9861cd615652e9b0509b6fc9a4dd0ddcaf318d3a1e5dcefa10"
PREDECESSOR_RUNNER_SHA256 = "d1a7344c13501251144913ba8c5fd75f387805a85fcf5066bbc8ebf4dee86574"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    predecessor = experiment.parent / PREDECESSOR_ID
    validate_experiment_archive(predecessor)
    manifest_path = predecessor / "experiment_manifest.json"
    runner_path = predecessor / "run_experiment.py"
    if _sha256(manifest_path) != PREDECESSOR_MANIFEST_SHA256:
        raise ValueError("EX47 failed manifest differs from frozen successor protocol")
    if _sha256(runner_path) != PREDECESSOR_RUNNER_SHA256:
        raise ValueError("EX47 runner differs from frozen successor protocol")

    source = runner_path.read_text(encoding="utf-8")
    source = source.replace(
        'EXPERIMENT_ID = "20260923_S008_EX47"',
        f'EXPERIMENT_ID = "{EXPERIMENT_ID}"',
    )
    old = '''        signal_dates = prices.index[(prices.index >= pd.Timestamp(row["evaluation_start"])) & (prices.index < pd.Timestamp(row["evaluation_end"]))]
        selected_targets = history.loc[signal_dates, "target_position"].astype("int8")
        if hashlib.sha256(selected_targets.to_numpy(dtype="int8").tobytes()).hexdigest() != row["behavior_sha256"]:
            raise ValueError(f"reconstructed behavior differs for {name}")
        positions[name] = pd.Series(selected_targets.to_numpy(), index=oracle_dates, dtype="int8")'''
    new = '''        evaluation_dates = prices.index[(prices.index >= pd.Timestamp(row["evaluation_start"])) & (prices.index <= pd.Timestamp(row["evaluation_end"]))]
        execution_positions = prices.index.get_indexer(evaluation_dates)
        if (execution_positions <= 0).any():
            raise ValueError(f"evaluation execution date misses prior signal for {name}")
        signal_dates = prices.index[execution_positions - 1]
        selected_targets = history.loc[signal_dates, "target_position"].astype("int8")
        if hashlib.sha256(selected_targets.to_numpy(dtype="int8").tobytes()).hexdigest() != row["behavior_sha256"]:
            raise ValueError(f"reconstructed behavior differs for {name}")
        executed = pd.Series(selected_targets.to_numpy(), index=evaluation_dates, dtype="int8")
        positions[name] = executed.reindex(oracle_dates)
        if positions[name].isna().any():
            raise ValueError(f"reconstructed execution positions miss oracle dates for {name}")'''
    if source.count(old) != 1:
        raise ValueError("EX47 repair target count differs from one")
    source = source.replace(old, new)
    source = source.replace("# S008 EX47", "# S008 EX48")
    namespace = {"__file__": str(Path(__file__).resolve()), "__name__": "s008_ex48_successor"}
    exec(compile(source, str(runner_path), "exec"), namespace)
    namespace["main"]()


if __name__ == "__main__":
    main()
