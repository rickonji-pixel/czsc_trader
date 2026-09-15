from __future__ import annotations

from typing import Any

from strategy_manager import canonical_sha256


REVIEW_FIELDS = {
    "schema_version",
    "strategy_id",
    "candidate_id",
    "candidate_hash",
    "decision",
    "reviewed_by",
    "rationale",
    "mechanism_review",
    "external_relevance_review",
    "deployment_review",
    "monitoring_plan_review",
    "reviewed_at",
}


def build_freeze_approval(
    *,
    machine_report: dict[str, Any],
    review: dict[str, Any],
    strategy_id: str,
    source_experiment: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Bind one explicit human review to one immutable SE machine report."""
    missing = REVIEW_FIELDS - review.keys()
    unknown = review.keys() - REVIEW_FIELDS
    if missing:
        raise ValueError(f"freeze review missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"freeze review contains unknown fields: {sorted(unknown)}")
    if review.get("schema_version") != 1:
        raise ValueError("freeze review schema_version must be 1")
    if review.get("decision") != "APPROVE_FREEZE":
        raise ValueError("freeze review decision must be APPROVE_FREEZE")
    if review.get("reviewed_by") != actor or review.get("rationale") != reason:
        raise ValueError("CLI actor and reason must match the human freeze review")
    if review.get("strategy_id") != strategy_id:
        raise ValueError("freeze review belongs to another strategy")
    for name in (
        "mechanism_review",
        "external_relevance_review",
        "deployment_review",
        "monitoring_plan_review",
    ):
        if review.get(name) != "APPROVED":
            raise ValueError(f"freeze review item must be APPROVED: {name}")
    if machine_report.get("schema_version") != 2:
        raise ValueError("freeze requires a schema v2 SE machine report")
    if machine_report.get("machine_verdict") != "ELIGIBLE_FOR_FREEZE_REVIEW":
        raise ValueError("SE machine report is not eligible for freeze review")
    if (
        review.get("candidate_id") != machine_report.get("candidate_id")
        or review.get("candidate_hash") != machine_report.get("candidate_hash")
    ):
        raise ValueError("freeze review and SE machine report candidate differ")
    report_without_hash = dict(machine_report)
    report_hash = report_without_hash.pop("report_hash", None)
    if report_hash is None or canonical_sha256(report_without_hash) != report_hash:
        raise ValueError("SE machine report hash mismatch")
    risk_label = machine_report.get("risk_label")
    if risk_label not in {"FAVORABLE", "MIXED"}:
        raise ValueError("SE machine report risk label is not freeze-review eligible")
    return {
        "schema_version": 2,
        "assessment_id": f"FR-{machine_report['report_id']}",
        "strategy_id": strategy_id,
        "candidate_id": review["candidate_id"],
        "candidate_hash": review["candidate_hash"],
        "decision": "APPROVE_FREEZE",
        "risk_label": risk_label,
        "source_experiment": source_experiment,
        "machine_report_id": machine_report["report_id"],
        "machine_report_hash": report_hash,
        "machine_verdict": machine_report["machine_verdict"],
        "reviewed_by": review["reviewed_by"],
        "rationale": review["rationale"],
        "mechanism_review": review["mechanism_review"],
        "external_relevance_review": review["external_relevance_review"],
        "deployment_review": review["deployment_review"],
        "monitoring_plan_review": review["monitoring_plan_review"],
        "reviewed_at": review["reviewed_at"],
    }
