from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX21"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _managed_directory(*, prefix: str, dir: Path):
    del prefix
    root = Path(dir).resolve()
    path = (root / "S008_EX21_runtime").resolve()
    if path.parent != root or path.name != "S008_EX21_runtime":
        raise ValueError("unsafe EX21 temporary path")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    try:
        yield str(path)
    finally:
        if path.exists():
            shutil.rmtree(path)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    overlay_path = experiment / "artifacts/protocol.json"
    overlay = _read(overlay_path)
    predecessor = repo / "experiments/S008/20260923_S008_EX20"
    base_protocol_path = predecessor / "artifacts/protocol.json"
    base_script_path = predecessor / "run_experiment.py"
    runtime_path = experiment / "runtime/strategy_runtime/strategies/s008_prototypes.py"
    paths = {
        "ex20_manifest_sha256": predecessor / "experiment_manifest.json",
        "base_protocol_sha256": base_protocol_path,
        "base_script_sha256": base_script_path,
        "runtime_file_sha256": runtime_path,
    }
    for key, path in paths.items():
        if _sha256(path) != overlay["sources"][key]:
            raise ValueError(f"frozen technical successor source differs: {path}")
    validate_experiment_archive(predecessor)

    specification = importlib.util.spec_from_file_location("s008_ex20_base", base_script_path)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load frozen EX20 implementation gate")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    base_read = module._read

    def inherited_read(path: Path) -> dict[str, object]:
        if path.resolve() != overlay_path.resolve():
            return base_read(path)
        protocol = base_read(base_protocol_path)
        protocol["experiment_id"] = EXPERIMENT_ID
        protocol["experiment_type"] = str(overlay["experiment_type"])
        protocol["predecessor_experiment_id"] = str(overlay["predecessor_experiment_id"])
        return protocol

    module._read = inherited_read
    module.EXPERIMENT_ID = EXPERIMENT_ID
    module.__file__ = str(Path(__file__).resolve())
    module.tempfile = SimpleNamespace(TemporaryDirectory=_managed_directory)
    module.main()

    for name in ("03_execution.md", "04_conclusion.md"):
        path = experiment / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("# S008 EX20", "# S008 EX21"),
            encoding="utf-8",
        )
    evidence = _read(experiment / "artifacts/implementation_evidence.json")
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": overlay["experiment_type"],
            "strategy_id": "S008",
            "credential_id": "SGC-S008-001",
            "symbol": "518880.SH",
            "development_cutoff": "2024-12-31",
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()

