from __future__ import annotations

import hashlib
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX08"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_new_returns"):
        raise ValueError("EX08 protocol identity or return declaration differs")
    ex07 = repo / str(protocol["sources"]["ex07_archive"])
    validate_experiment_archive(ex07)
    if _sha256(ex07 / "experiment_manifest.json") != protocol["sources"]["ex07_manifest_sha256"]:
        raise ValueError("EX07 manifest differs from amendment protocol")
    if _sha256(ex07 / "artifacts/protocol.json") != protocol["sources"]["ex07_protocol_sha256"]:
        raise ValueError("EX07 protocol differs from amendment protocol")
    original = _read(ex07 / "artifacts/protocol.json")
    amendment = protocol["amendment"]
    if original["normalization"]["minimum_observations"] != amendment["before"]:
        raise ValueError("amendment before-value differs from EX07")
    effective = json.loads(json.dumps(original))
    effective["normalization"]["minimum_observations"] = amendment["after"]
    _write(artifacts / "effective_search_protocol.json", effective)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "PROCEED_TO_PREREGISTERED_PARAMETER_SEARCH",
        "changed_fields": [amendment["field"]],
        "reads_new_returns": False,
        "search_started": False,
        "candidate_created": False,
    }
    _write(artifacts / "amendment_evidence.json", evidence)
    (experiment / "03_execution.md").write_text("# S007 EX08 执行\n\n状态：`COMPLETE`。仅生成修订后的有效搜索协议，没有读取收益或运行搜索。\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text("# S007 EX08 结论\n\n裁决：`PROCEED_TO_PREREGISTERED_PARAMETER_SEARCH`。暖机期从126日改为20日，避免人为压低完整窗口收益；全部其他原型合同保持EX07口径。\n", encoding="utf-8")
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": protocol["strategy_id"], "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": evidence["decision"], "promotion_allowed": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
