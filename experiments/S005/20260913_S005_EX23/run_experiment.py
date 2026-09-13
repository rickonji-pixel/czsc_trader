from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX23"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/data_quality.json": source_spec["quality_sha256"],
        source / "artifacts/margin_detail.csv.gz": source_spec["margin_sha256"],
        source / "artifacts/component_margin_panel.csv.gz": source_spec[
            "component_panel_sha256"
        ],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    source_quality = _read(source / "artifacts/data_quality.json")
    margin = pd.read_csv(source / "artifacts/margin_detail.csv.gz")
    core = [str(value) for value in protocol["core_financing_fields"]]
    core_values = margin[core].apply(pd.to_numeric, errors="coerce")
    missing = {column: int(core_values[column].isna().sum()) for column in core}
    negative = {column: int(core_values[column].lt(0).sum()) for column in core}
    finite = bool(np.isfinite(core_values.to_numpy(dtype=float)).all())
    source_structure_pass = bool(
        source_quality["etf_session_coverage"] >= 0.95
        and source_quality["component_weight_coverage"] >= 0.90
        and source_quality["component_daily_weight_coverage_p10"] >= 0.85
        and source_quality["duplicate_security_date_rows"] == 0
        and source_quality["causal_membership"]
        and source_quality["api_calls"] <= 150
    )
    core_pass = finite and not any(missing.values()) and not any(negative.values())
    passed = source_structure_pass and core_pass
    review = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "source_experiment": source_spec["experiment_id"],
        "source_reported_pass": source_quality["passed"],
        "source_structure_pass": source_structure_pass,
        "core_financing_fields": core,
        "core_missing_counts": missing,
        "core_negative_counts": negative,
        "core_finite": finite,
        "secondary_field_disclosures": {
            "rqye_missing": int(margin["rqye"].isna().sum()),
            "rzrqye_missing": int(margin["rzrqye"].isna().sum()),
            "rqchl_negative": int(margin["rqchl"].lt(0).sum()),
        },
        "passed": passed,
    }
    (artifacts / "reviewed_data_quality.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    status = "PASS" if passed else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S005 EX23 执行\n\n"
        f"字段范围纠偏：`{status}`。融资余额、融资买入和融资偿还的缺失分别为"
        f"{list(missing.values())}，负值分别为{list(negative.values())}。融券字段缺省与1条"
        "冲正记录仅披露，不进入后续融资机制。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_MARGIN_MECHANISM_PREREGISTRATION" if passed else "STOP_MARGIN_ROUTE_ON_REVIEWED_DATA"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX23 结论\n\n"
        f"裁决：`{decision}`。纠偏只限定可用字段，不构成收益证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": "2026-09-02",
            "status": status,
            "reads_post_event_prices": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
