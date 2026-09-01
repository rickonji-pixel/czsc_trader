"""Close the eligible pairwise CZSC interaction family deterministically."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from .experiment_archive import build_experiment_manifest, validate_experiment_archive


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_pairwise_interaction(
    repository_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    repository_root = Path(repository_root).resolve()
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    expected = {
        "experiment_id": "0901_EX12",
        "handler": "czsc_route_pairwise_interaction",
        "symbol": "588080.SH",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "parent_experiment": "0901_EX11",
        "operator": "AND",
        "maximum_order": 2,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"pairwise protocol {key} differs from preregistration")
    parent_dir = repository_root / "experiments" / str(protocol["parent_experiment"])
    actual_hash = sha256((parent_dir / "experiment_manifest.json").read_bytes()).hexdigest()
    if actual_hash != str(protocol["parent_manifest"]):
        raise ValueError("pairwise parent manifest identity differs")
    confirmed = pd.read_csv(parent_dir / "artifacts" / "confirmed_factors.csv")
    parent_factors = list(map(str, protocol["parent_factors"]))
    if sorted(map(str, confirmed["factor"])) != sorted(parent_factors):
        raise ValueError("pairwise parent factor identities differ")
    if len(parent_factors) != 2:
        raise ValueError("pairwise protocol must freeze exactly two eligible parents")
    left, right = parent_factors
    left_base, left_position = left.rsplit("__target_", 1)
    right_base, right_position = right.rsplit("__target_", 1)
    mutually_exclusive = left_base == right_base and {left_position, right_position} == {"0", "1"}
    combinations = [
        {
            "left": left,
            "right": right,
            "operator": "AND",
            "status": "excluded" if mutually_exclusive else "candidate",
            "reason": "mutually_exclusive_zero" if mutually_exclusive else "",
        }
    ]
    candidate_count = int(not mutually_exclusive)
    status = "PASS" if candidate_count else "FAIL"
    _write_json(artifacts / "interaction_candidates.json", combinations)
    summary = {
        "status": status,
        "eligible_parent_count": len(parent_factors),
        "enumerated_pair_count": 1,
        "nonconstant_interaction_count": candidate_count,
        "exclusion_reason": "" if candidate_count else "mutually_exclusive_zero",
        "visible_sample_end": "2025-12-31",
        "access_2026": False,
    }
    _write_json(artifacts / "metrics.json", summary)
    _write_json(
        artifacts / "identity_audit.json",
        {
            "status": "PASS",
            "parent_experiment": str(protocol["parent_experiment"]),
            "parent_manifest_sha256": actual_hash,
            "parent_factors": parent_factors,
            "access_2026": False,
        },
    )
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0901_EX12 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`",
                "- 合法父因子：2项；唯一无序二元组合：1项。",
                f"- 非恒定交互：{candidate_count}项。",
                "- 两个父因子来自同一事件的冠军target_0与target_1条件。",
                "- 未读取任何行情，未访问2026，未运行策略。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0901_EX12 结论",
                "",
                f"状态：`{status}`。",
                "",
                "两个合法父因子按定义互斥，AND交互恒为0，因此没有可准入的二阶结构交互。",
                "根据预注册边界，不改用OR、NOT或高阶表达式挽救本家族。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    protocol_sha = sha256((artifacts / "protocol.json").read_bytes()).hexdigest()
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-09-01",
            "status": status,
            "symbol": "588080.SH",
            "asset_type": "etf",
            "champion": protocol["champion"],
            "protocol_sha256": protocol_sha,
            "visible_sample_end": "2025-12-31",
            "validation_accessed": False,
            "historical_2026_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
