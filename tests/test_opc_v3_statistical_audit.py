import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EX06 = ROOT / "experiments" / "0903_EX06"


def test_ex06_reuses_ex05_pool_under_opc_v3() -> None:
    protocol = json.loads((EX06 / "evaluation_protocol.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("ex06", EX06 / "run_experiment.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = json.loads(
        (ROOT / "experiments" / "0903_EX05" / "candidate_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = module.candidate_manifest_from_source(source)

    assert protocol["standard_version"] == "opc-v3"
    assert protocol["development_cutoff"] == "2026-09-02"
    assert manifest["reuse_source_experiments"] == ["0903_EX05"]
    assert len(manifest["candidates"]) == 1_187
    assert manifest["audit_protocol"] == {
        "version": "champion-audit-v1",
        "seed": 20260903,
        "bootstrap_repetitions": 10_000,
        "mean_block_lengths": [21, 10, 42],
    }

    runner = (EX06 / "run_experiment.py").read_text(encoding="utf-8")
    assert "evaluation_was_complete" in runner
    assert "benchmark_path.read_text" in runner
