"""Investment-director commands for candidate review, evaluation and freeze."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from strategy_manager import (
    EvaluationMandate,
    StrategyRegistry,
    StrategyVersion,
    canonical_sha256,
)

from .candidate_package import CandidatePackage, load_candidate_package
from .context import RepositoryContext
from .errors import ValidationError
from .freeze_review_service import (
    evaluate_freeze_review,
    freeze_review_candidate,
    open_freeze_review,
)
from .results import CommandResult
from .research_registration import (
    open_research_credential,
    promote_research_registration,
    rollback_research_promotion,
)
from .runtime_acceptance import prospective_release


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _candidate_parts(reference: str) -> tuple[str, str]:
    strategy_id, separator, candidate_id = reference.partition("-")
    if (
        not separator
        or not strategy_id.startswith("S")
        or not strategy_id[1:].isdigit()
        or not candidate_id
        or candidate_id.startswith("v")
    ):
        raise ValueError(f"invalid candidate id: {reference}")
    return strategy_id, candidate_id


def _resolve_path(context: RepositoryContext, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (context.root / path).resolve()


def _candidate_directory(context: RepositoryContext, reference: str) -> Path:
    strategy_id, _candidate_id = _candidate_parts(reference)
    return context.strategy_root / strategy_id / "candidates" / reference


def _review_record(directory: Path) -> dict[str, Any]:
    record = _read_object(directory / "review.json")
    record_hash = record.pop("record_hash", None)
    if record_hash != canonical_sha256(record):
        raise ValueError("candidate review record hash mismatch")
    record["record_hash"] = record_hash
    return record


def _governed_candidate(
    context: RepositoryContext, reference: str,
) -> tuple[CandidatePackage, EvaluationMandate, dict[str, Any], Path]:
    directory = _candidate_directory(context, reference)
    record = _review_record(directory)
    package = load_candidate_package(directory / "package")
    if package.candidate_reference != reference or record.get("package_hash") != package.package_hash:
        raise ValueError("candidate review identity differs from governed package")
    mandate = EvaluationMandate.from_dict(_read_object(directory / "evaluation_mandate.json"))
    if (
        mandate.strategy_id != package.snapshot.strategy_id
        or mandate.candidate_id != package.snapshot.candidate_id
        or record.get("mandate_hash") != mandate.mandate_hash
    ):
        raise ValueError("candidate review mandate identity differs")
    return package, mandate, record, directory


def review_candidate(
    context: RepositoryContext, package_path: Path, mandate_path: Path,
) -> CommandResult:
    """Validate and atomically admit one researcher-authored candidate package."""

    try:
        source = load_candidate_package(_resolve_path(context, package_path))
        expected_parent = (
            context.root / "research" / source.snapshot.strategy_id / "candidates"
        ).resolve()
        if source.root.parent != expected_parent or source.root.name != source.candidate_reference:
            raise ValueError(
                "candidate package must use research/<strategy>/candidates/<candidate-id>"
            )
        mandate = EvaluationMandate.from_dict(
            _read_object(_resolve_path(context, mandate_path))
        )
        if (
            mandate.strategy_id != source.snapshot.strategy_id
            or mandate.candidate_id != source.snapshot.candidate_id
        ):
            raise ValueError("candidate package and evaluation mandate identities differ")
        research_credential = open_research_credential(
            context, source.snapshot.strategy_id
        )
        credential, promotion = promote_research_registration(
            context,
            source.snapshot.strategy_id,
            research_credential.credential_id,
        )
        destination = _candidate_directory(context, source.candidate_reference)
        created = False
        if destination.exists():
            governed, stored_mandate, record, _directory = _governed_candidate(
                context, source.candidate_reference
            )
            if (
                governed.package_hash != source.package_hash
                or stored_mandate.to_dict() != mandate.to_dict()
                or record.get("credential_id") != credential.credential_id
            ):
                raise ValueError("candidate id is already governed by different content")
        else:
            temp_root = context.root / ".tmp"
            temp_root.mkdir(parents=True, exist_ok=True)
            stage = temp_root / f"candidate-review-{uuid4().hex}"
            stage.mkdir()
            try:
                shutil.copytree(source.root, stage / "package")
                _write_object(stage / "evaluation_mandate.json", mandate.to_dict())
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stage), str(destination))
                created = True
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        governed = load_candidate_package(destination / "package")
        result = open_freeze_review(
            context,
            credential_id=credential.credential_id,
            candidate_path=destination / "package" / governed.manifest["candidate_snapshot"],
            mandate_path=destination / "evaluation_mandate.json",
            actor=mandate.finalized_by,
            reason="CANDIDATE_REVIEWED",
            runtime_root=governed.runtime_root,
            candidate_package={
                "candidate_id": governed.candidate_reference,
                "package_hash": governed.package_hash,
                "manifest": "candidate_submission.json",
            },
        )
        record_payload = {
            "schema_version": 1,
            "candidate_id": governed.candidate_reference,
            "candidate_hash": governed.snapshot.candidate_hash,
            "package_hash": governed.package_hash,
            "mandate_hash": mandate.mandate_hash,
            "credential_id": credential.credential_id,
        }
        _write_object(
            destination / "review.json",
            {**record_payload, "record_hash": canonical_sha256(record_payload)},
        )
    except Exception as exc:
        if "created" in locals() and created and "destination" in locals() and destination.exists():
            shutil.rmtree(destination)
        if "promotion" in locals():
            rollback_research_promotion(context, promotion)
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(
            "candidate_review_failed", str(exc), context={"command": "candidate.review"}
        ) from exc
    return CommandResult(
        "PASS",
        "candidate.review",
        {
            "candidate_id": governed.candidate_reference,
            "candidate_state": "REVIEWED",
            "candidate_hash": governed.snapshot.candidate_hash,
            "package_hash": governed.package_hash,
        },
        result.artifacts,
    )


def evaluate_candidate(context: RepositoryContext, reference: str) -> CommandResult:
    try:
        package, _mandate, record, _directory = _governed_candidate(context, reference)
        evaluated = evaluate_freeze_review(
            context,
            package.snapshot.strategy_id,
            str(record["credential_id"]),
            runtime_root=package.runtime_root,
            chart_descriptor=dict(package.runtime_binding["charts"]),
        )
        report = evaluated.result["adjudication_report"]
        verdict = {
            "ELIGIBLE_FOR_FREEZE_REVIEW": "ELIGIBLE",
            "REJECTED": "INELIGIBLE",
            "INCOMPLETE": "INSUFFICIENT",
        }[str(report["machine_verdict"])]
    except Exception as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(
            "candidate_evaluation_failed",
            str(exc),
            context={"command": "candidate.evaluate", "candidate_id": reference},
        ) from exc
    return CommandResult(
        "PASS",
        "candidate.evaluate",
        {
            "candidate_id": reference,
            "candidate_state": "EVALUATED",
            "verdict": verdict,
            "report_hash": report["report_hash"],
        },
        evaluated.artifacts,
    )


def _prospective_version(
    registry: StrategyRegistry,
    package: CandidatePackage,
    mandate: EvaluationMandate,
) -> StrategyVersion:
    versions = registry.versions(package.snapshot.strategy_id)
    version = f"v{len(versions) + 1}"
    governance = {
        "credential_id": "PROSPECTIVE",
        "candidate_submission_seal_hash": "0" * 64,
        "adjudication_seal_hash": "0" * 64,
        "approval_seal_hash": "0" * 64,
        "candidate_snapshot_hash": "0" * 64,
        "evaluation_mandate_hash": "0" * 64,
        "adjudication_report_hash": "0" * 64,
    }
    return StrategyVersion.from_dict(
        {
            "schema_version": 3,
            "strategy_id": package.snapshot.strategy_id,
            "version": version,
            "release_id": f"{package.snapshot.strategy_id}-{version}",
            "parent_version": versions[-1].version if versions else None,
            "change_summary": "prospective candidate freeze",
            "source_experiment": package.snapshot.source_experiment,
            "source_candidate": package.snapshot.candidate_id,
            "selection_data_cutoff": mandate.development_cutoff,
            "forward_start": mandate.forward_start,
            "strategy_payload": package.snapshot.strategy_payload,
            "release_hash": None,
            "governance": governance,
            "governance_hash": canonical_sha256(governance),
        }
    )


def _build_release_package(
    context: RepositoryContext,
    package: CandidatePackage,
    mandate: EvaluationMandate,
) -> tuple[Path, str, bool]:
    registry = StrategyRegistry(context.strategy_root)
    release = prospective_release(_prospective_version(registry, package, mandate))
    strategy_version_id = release.release_id
    destination = (
        context.strategy_root
        / package.snapshot.strategy_id
        / "releases"
        / release.version
    )
    if destination.exists():
        manifest = _read_object(destination / "release_manifest.json")
        if (
            manifest.get("strategy_version_id") != strategy_version_id
            or manifest.get("strategy_version_hash") != release.release_hash
            or manifest.get("candidate_package_hash") != package.package_hash
        ):
            raise ValueError("strategy version package already exists with different content")
        return destination, strategy_version_id, False

    temp_root = context.root / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    stage = temp_root / f"strategy-freeze-{uuid4().hex}"
    stage.mkdir()
    try:
        runtime_destination = stage / "runtime" / "strategy_runtime"
        shutil.copytree(package.runtime_root, runtime_destination)
        binding = {
            "schema_version": 1,
            "release_id": strategy_version_id,
            "release_hash": release.release_hash,
            "source_files": list(package.runtime_binding["source_files"]),
            "implementation_sha256": package.runtime_binding["implementation_sha256"],
            "install_files": list(package.runtime_binding["install_files"]),
            "charts": package.runtime_binding["charts"],
            "observation": package.runtime_binding["observation"],
        }
        _write_object(stage / "runtime_binding.json", binding)
        files = {
            item.relative_to(stage).as_posix(): _file_sha256(item)
            for item in sorted(stage.rglob("*"))
            if item.is_file()
        }
        manifest_payload = {
            "schema_version": 1,
            "strategy_version_id": strategy_version_id,
            "strategy_version_hash": release.release_hash,
            "source_candidate_id": package.candidate_reference,
            "candidate_package_hash": package.package_hash,
            "runtime_root": "runtime/strategy_runtime",
            "runtime_binding": "runtime_binding.json",
            "files": files,
        }
        _write_object(
            stage / "release_manifest.json",
            {**manifest_payload, "package_hash": canonical_sha256(manifest_payload)},
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(destination))
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination, strategy_version_id, True


def freeze_candidate(
    context: RepositoryContext, reference: str, change_summary: str,
) -> CommandResult:
    release_directory: Path | None = None
    created = False
    try:
        if not isinstance(change_summary, str) or not change_summary.strip():
            raise ValueError("change summary must be nonblank")
        package, mandate, record, _directory = _governed_candidate(context, reference)
        release_directory, expected_id, created = _build_release_package(
            context, package, mandate
        )
        frozen = freeze_review_candidate(
            context,
            package.snapshot.strategy_id,
            str(record["credential_id"]),
            actor=mandate.finalized_by,
            reason="APPROVE_FREEZE",
            change_summary=change_summary.strip(),
            runtime_root=release_directory / "runtime" / "strategy_runtime",
        )
        version = frozen.result["version"]
        if version["release_id"] != expected_id:
            raise ValueError("frozen strategy version differs from prepared package")
    except Exception as exc:
        if created and release_directory is not None and release_directory.exists():
            shutil.rmtree(release_directory)
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(
            "candidate_freeze_failed",
            str(exc),
            context={"command": "candidate.freeze", "candidate_id": reference},
        ) from exc
    return CommandResult(
        "PASS",
        "candidate.freeze",
        {
            "candidate_id": reference,
            "candidate_state": "FROZEN",
            "strategy_version_id": version["release_id"],
            "strategy_version_hash": version["release_hash"],
        },
    )
