"""Re-evaluate the immutable EX04 candidate pool under OPC-v2."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.evaluation_service import evaluate_experiment
from czsc_trader.identity import normalized_text_sha256


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
SOURCE_MANIFEST = REPO_ROOT / "experiments" / "0903_EX04" / "candidate_manifest.json"
MANIFEST_PATH = EXPERIMENT_DIR / "candidate_manifest.json"
PREPARED_FROM_COMMIT = "9e7037ca5e6abd35f22a8368178541d4d423900b"
SOURCE_MANIFEST_SHA256 = "e448a53677b66aaab101019b5ba5ee84f975b694797329b9c1f9fdbdaf9f6762"


def candidate_manifest_from_source(source: dict[str, object]) -> dict[str, object]:
    manifest = copy.deepcopy(source)
    manifest["experiment_id"] = "0903_EX05"
    manifest["prepared_from_commit"] = PREPARED_FROM_COMMIT
    manifest["source_experiment"] = {
        "experiment_id": "0903_EX04",
        "candidate_manifest_sha256": SOURCE_MANIFEST_SHA256,
    }
    trials = manifest.get("trials")
    if not isinstance(trials, list):
        raise ValueError("EX04 candidate manifest has no trial ledger")
    for trial in trials:
        if not isinstance(trial, dict) or "candidate_id" not in trial:
            raise ValueError("EX04 candidate manifest contains an invalid trial")
        trial["trial_id"] = f"EX05-{trial['candidate_id']}"
    audits = manifest.get("audits")
    if not isinstance(audits, dict):
        raise ValueError("EX04 candidate manifest has no audits")
    audits["revaluation_only"] = True
    audits["candidate_payloads_unchanged"] = True
    return manifest


def prepare_manifest() -> dict[str, object]:
    actual_hash = normalized_text_sha256(SOURCE_MANIFEST)
    if actual_hash != SOURCE_MANIFEST_SHA256:
        raise ValueError("EX04 candidate manifest differs from preregistration")
    source = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    if source.get("experiment_id") != "0903_EX04":
        raise ValueError("source candidate manifest is not EX04")
    manifest = candidate_manifest_from_source(source)
    candidates = manifest.get("candidates")
    trials = manifest.get("trials")
    if not isinstance(candidates, list) or len(candidates) != 1187:
        raise ValueError("EX05 requires all 1,187 EX04 candidates")
    if not isinstance(trials, list) or len(trials) != len(candidates):
        raise ValueError("EX05 requires a complete relabeled trial ledger")
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def run() -> dict[str, object]:
    prepare_manifest()
    context = RepositoryContext.discover(REPO_ROOT, explicit_root=REPO_ROOT)
    return dict(evaluate_experiment(context, "0903_EX05").result)


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
