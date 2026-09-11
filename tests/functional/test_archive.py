from __future__ import annotations

from datetime import date
from pathlib import Path

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    create_experiment_dir,
    resolve_experiment_dir,
)

from functional_support import invoke_main, invoke_main_failure


def test_ft_t07_archive_validation_is_portable_and_detects_tampering(
    functional_repo: Path, capsys
) -> None:
    archive = functional_repo / "experiments" / "0904_ARCHIVE"
    archive.mkdir()
    for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
        (archive / name).write_text("document\n", encoding="utf-8")
    runner = archive / "run_experiment.py"
    runner.write_bytes(b"print('research')\n")
    cache = archive / "__pycache__"
    cache.mkdir()
    bytecode = cache / "run_experiment.cpython-312.pyc"
    bytecode.write_bytes(b"runtime-cache")
    manifest = build_experiment_manifest(
        archive, {"experiment_id": "0904_ARCHIVE", "status": "COMPLETE"}
    )
    runner.write_bytes(b"print('research')\r\n")
    command = [
        "archive",
        "validate",
        "--archive",
        str(archive),
        "--repo-root",
        str(functional_repo),
    ]

    first = invoke_main(command, capsys)
    assert first["result"] == {
        "validated_count": 1,
        "experiments": ["0904_ARCHIVE"],
    }
    assert "__pycache__/run_experiment.cpython-312.pyc" not in manifest["files"]
    bytecode.write_bytes(b"changed-runtime-cache")
    (archive / "evaluation_acceptance.json").write_text(
        '{"status":"PAPER_ACTIVE"}\n', encoding="utf-8"
    )
    assert invoke_main(command, capsys)["status"] == "PASS"

    (archive / "04_conclusion.md").write_text("tampered\n", encoding="utf-8")
    failure = invoke_main_failure(command, capsys)
    assert failure["error"]["code"] == "experiment_archive_invalid"
    assert "04_conclusion.md" in failure["error"]["message"]

    experiment_root = functional_repo / "new-experiments"
    first = create_experiment_dir(experiment_root, date(2026, 9, 9), "S002")
    second = create_experiment_dir(experiment_root, date(2026, 9, 9), "S002")
    assert first.parent == experiment_root / "S002"
    assert first.name == "20260909_S002_EX01"
    assert second.name == "20260909_S002_EX02"
    for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
        (first / name).write_text("document\n", encoding="utf-8")
    build_experiment_manifest(
        first,
        {
            "experiment_id": first.name,
            "status": "COMPLETE",
            "strategy_id": "S002",
            "symbol": "510500.SH",
            "development_cutoff": "2026-09-08",
        },
    )
    assert resolve_experiment_dir(experiment_root, first.name) == first.resolve()


def test_ft_t07_archive_all_discovers_strategy_owned_directories(
    functional_repo: Path, capsys
) -> None:
    experiments = functional_repo / "experiments"
    created: list[str] = []
    for strategy_id in ("S001", "S002"):
        archive = create_experiment_dir(experiments, date(2026, 9, 11), strategy_id)
        created.append(archive.name)
        for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
            (archive / name).write_text("document\n", encoding="utf-8")
        build_experiment_manifest(
            archive,
            {
                "experiment_id": archive.name,
                "status": "COMPLETE",
                "strategy_id": strategy_id,
                "symbol": "588080.SH" if strategy_id == "S001" else "510500.SH",
                "development_cutoff": "2026-09-10",
            },
        )

    result = invoke_main(
        ["archive", "validate", "--all", "--repo-root", str(functional_repo)],
        capsys,
    )
    assert result["result"] == {
        "validated_count": 2,
        "experiments": created,
    }
