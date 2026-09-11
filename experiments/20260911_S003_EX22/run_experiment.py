from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.relative_style_events import (
    build_relative_style_features,
    generate_relative_style_events,
)


EXPERIMENT_ID = "20260911_S003_EX22"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("conditional_return_analysis"):
        raise ValueError("EX22 may not read conditional forward returns")

    source = repo_root / "experiments" / protocol["dataset"]["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": protocol["dataset"]["source_manifest_sha256"],
        source / "artifacts" / "aligned_adjusted_daily.csv": protocol["dataset"]["aligned_panel_sha256"],
        source / "artifacts" / "alignment_audit.json": protocol["dataset"]["alignment_audit_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    panel = pd.read_csv(source / "artifacts" / "aligned_adjusted_daily.csv")
    features = build_relative_style_features(panel)
    threshold = protocol["threshold"]
    events, density, annual, overlap = generate_relative_style_events(
        features,
        evaluation_start=protocol["dataset"]["evaluation_start"],
        threshold_lookback=int(threshold["lookback_sessions"]),
        tail_quantile=float(threshold["tail_quantile"]),
    )
    features.to_csv(
        artifacts / "relative_features.csv.gz", index=False, compression="gzip", lineterminator="\n"
    )
    events.to_csv(artifacts / "mechanism_events.csv", index=False, lineterminator="\n")
    density.to_csv(artifacts / "density_metrics.csv", index=False, lineterminator="\n")
    annual.to_csv(artifacts / "annual_event_counts.csv", index=False, lineterminator="\n")
    overlap.to_csv(artifacts / "event_overlap.csv", index=True, lineterminator="\n")

    eligible = density.loc[density["density_eligible"], "mechanism"].tolist()
    summary = {
        "experiment_id": EXPERIMENT_ID,
        "eligible_mechanisms": eligible,
        "eligible_count": len(eligible),
        "all_mechanisms_pass": len(eligible) == len(density),
        "forward_return_columns": [
            column for column in features if "forward" in column.lower() or "return" in column.lower()
        ],
    }
    if summary["forward_return_columns"]:
        raise AssertionError("feature artifact contains a forbidden forward-return column")
    _write_json(artifacts / "census_summary.json", summary)

    lines = [
        "# S003 EX22 执行",
        "",
        "本轮只生成当时可知的相对风格特征和事件标记，没有计算任何事件后的收益。密度结果：",
        "",
    ]
    for row in density.itertuples(index=False):
        lines.append(
            f"- `{row.mechanism}`：{row.event_count}个事件，滚动60日中位数"
            f"{row.rolling_60_median:.1f}，第10百分位{row.rolling_60_p10:.1f}，"
            f"资格={'通过' if row.density_eligible else '未通过'}。"
        )
    (experiment / "03_execution.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX22 结论\n\n"
        f"共有{len(eligible)}条机制通过密度门：{eligible}。通过者只获得下一轮收益评价资格。"
        "当前没有任何盈利、稳健或候选结论。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": protocol["dataset"]["development_cutoff"],
            "status": "PASS" if eligible else "NO_ELIGIBLE_MECHANISM",
            "eligible_mechanisms": eligible,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
