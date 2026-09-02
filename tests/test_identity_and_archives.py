from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_portable_identity_separates_json_text_and_raw_bytes(tmp_path: Path) -> None:
    from czsc_trader.identity import (
        canonical_json_sha256,
        normalized_text_sha256,
        raw_file_sha256,
    )

    first_json = tmp_path / "first.json"
    second_json = tmp_path / "second.json"
    first_json.write_bytes('{\r\n  "b": "中",\r\n  "a": 1\r\n}\r\n'.encode("utf-8"))
    second_json.write_bytes('{"a":1,"b":"中"}\n'.encode("utf-8"))

    expected_json = "2831299868169bc527f55f88ebbdcd8b785d78d9e7dc64e6887dfbd2825dd247"
    assert canonical_json_sha256(first_json) == expected_json
    assert canonical_json_sha256(second_json) == expected_json
    assert canonical_json_sha256({"b": "中", "a": 1}) == expected_json
    assert raw_file_sha256(first_json) != raw_file_sha256(second_json)

    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    cr = tmp_path / "cr.txt"
    lf.write_bytes(b"alpha\nbeta\n")
    crlf.write_bytes(b"alpha\r\nbeta\r\n")
    cr.write_bytes(b"alpha\rbeta\r")
    expected_text = "e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee"

    assert normalized_text_sha256(lf) == expected_text
    assert normalized_text_sha256(crlf) == expected_text
    assert normalized_text_sha256(cr) == expected_text
    assert len({raw_file_sha256(path) for path in (lf, crlf, cr)}) == 3


def test_experiment_archive_normalizes_python_line_endings(tmp_path: Path) -> None:
    from czsc_trader.experiment_archive import (
        build_experiment_manifest,
        validate_experiment_archive,
    )

    for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
        (tmp_path / name).write_text("document\n", encoding="utf-8")
    runner = tmp_path / "run_experiment.py"
    runner.write_bytes(b"print('research')\n")
    build_experiment_manifest(tmp_path, {"experiment_id": "test"})

    runner.write_bytes(b"print('research')\r\n")

    validate_experiment_archive(tmp_path)


def test_experiment_archive_ignores_runtime_python_cache(tmp_path: Path) -> None:
    from czsc_trader.experiment_archive import (
        build_experiment_manifest,
        validate_experiment_archive,
    )

    for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
        (tmp_path / name).write_text("document\n", encoding="utf-8")
    (tmp_path / "run_experiment.py").write_text("print('research')\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    bytecode = cache / "run_experiment.cpython-312.pyc"
    bytecode.write_bytes(b"first-runtime-cache")

    manifest = build_experiment_manifest(tmp_path, {"experiment_id": "test"})

    assert "__pycache__/run_experiment.cpython-312.pyc" not in manifest["files"]
    bytecode.write_bytes(b"changed-runtime-cache")
    validate_experiment_archive(tmp_path)


def test_repository_eol_contract_is_lf_with_raw_and_binary_exceptions() -> None:
    completed = subprocess.run(
        [
            "git",
            "check-attr",
            "text",
            "eol",
            "--",
            "README.md",
            "configs/execution_policies/execution_policy_20260902.json",
            "data/raw/588080_daily_2026.csv",
            "artifacts/example.png",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    attributes = {
        (parts[0], parts[1]): parts[2]
        for line in completed.stdout.splitlines()
        if len(parts := line.split(": ", 2)) == 3
    }
    assert attributes[("README.md", "text")] == "auto"
    assert attributes[("README.md", "eol")] == "lf"
    assert attributes[
        ("configs/execution_policies/execution_policy_20260902.json", "eol")
    ] == "lf"
    assert attributes[("data/raw/588080_daily_2026.csv", "text")] == "unset"
    assert attributes[("artifacts/example.png", "text")] == "unset"


def test_dataflows_package_imports_outside_the_checkout(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import dataflows; "
                "print(Path(dataflows.__file__).resolve())"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    module_path = Path(completed.stdout.strip())
    assert module_path.is_relative_to(REPO_ROOT / "packages" / "dataflows")


def test_active_identities_resolve_after_cross_platform_lf_checkout(tmp_path: Path) -> None:
    from czsc_trader.baselines import resolve_baseline
    from czsc_trader.execution_policies import resolve_execution_policy

    relative_files = (
        "configs/rule_baselines/registry.json",
        "configs/rule_baselines/baseline_20260823.json",
        "configs/rule_baselines/baseline_20260826.json",
        "configs/rule_baselines/baseline_20260901.json",
        "configs/execution_policies/registry.json",
        "configs/execution_policies/execution_policy_20260902.json",
        "experiments/0824_EX04/artifacts/frozen_challenger.json",
        "experiments/0901_EX20/artifacts/frozen_challenger.json",
        "experiments/0902_EX02/artifacts/frozen_execution_policy.json",
    )
    for relative in relative_files:
        source = REPO_ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))

    baseline = resolve_baseline(
        tmp_path / "configs" / "rule_baselines",
        "baseline_20260901",
        symbol="588080.SH",
    )
    policy = resolve_execution_policy(
        tmp_path / "configs" / "execution_policies",
        symbol="588080.SH",
        baseline_version=baseline.version,
        baseline_sha256=baseline.sha256,
        required=True,
    )

    assert baseline.sha256 == "711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993"
    assert policy is not None
    assert policy.version == "execution_policy_20260902"


@pytest.mark.archive
def test_all_frozen_experiment_archives_validate() -> None:
    from czsc_trader.application.archive_service import validate_archives
    from czsc_trader.application.context import RepositoryContext

    result = validate_archives(
        RepositoryContext.discover(REPO_ROOT),
        all_archives=True,
    )

    assert result.status == "PASS"
    assert result.result["validated_count"] == 48
    assert result.result["experiments"][-3:] == [
        "0901_EX21",
        "0902_EX01",
        "0902_EX02",
    ]
