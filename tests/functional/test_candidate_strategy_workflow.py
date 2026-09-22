from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest
from strategy_manager import canonical_sha256
from strategy_runtime.implementation_identity import implementation_sha256

from czsc_trader.application import candidate_service
from czsc_trader.application.candidate_package import load_candidate_package
from czsc_trader.application.candidate_service import review_candidate
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.errors import ValidationError
from czsc_trader.application.results import CommandResult
from czsc_trader.application.research_governance_service import create_research_batch
from czsc_trader.application.strategy_runtime_service import (
    deploy_strategy,
    list_installed_strategies,
    strategy_info,
)
from czsc_trader.cli.main import build_parser


ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _candidate_package(root: Path) -> Path:
    package = root / "research" / "S990" / "candidates" / "S990-C001"
    runtime = package / "runtime" / "strategy_runtime"
    strategies = runtime / "strategies"
    charts = runtime / "charts"
    strategies.mkdir(parents=True)
    charts.mkdir(parents=True)
    shutil.copy2(ROOT / "tests" / "fixtures" / "candidate_runtime.py", strategies / "candidate_fixture.py")
    (charts / "__init__.py").write_text("", encoding="utf-8")
    (charts / "candidate_fixture.py").write_text(
        "class CandidateCharts:\n"
        "    @staticmethod\n"
        "    def render_backtest(context):\n"
        "        return '<html>backtest</html>'\n"
        "    @staticmethod\n"
        "    def render_forward_observation(context):\n"
        "        return '<html>forward</html>'\n",
        encoding="utf-8",
    )
    source_files = ("strategies/candidate_fixture.py",)
    source_hash = implementation_sha256(source_files, source_root=runtime)
    chart_files = ("charts/candidate_fixture.py",)
    chart_hash = implementation_sha256(chart_files, source_root=runtime)
    payload = {
        "runtime": {
            "module": "strategy_runtime.strategies.candidate_fixture",
            "qualname": "CandidateFixture",
            "contract_version": 1,
            "source_files": list(source_files),
            "source_sha256": source_hash,
        },
        "parameters": {"threshold": 0.5},
    }
    snapshot_payload = {
        "schema_version": 1,
        "strategy_id": "S990",
        "candidate_id": "C001",
        "source_experiment": "experiments/S990/EX01",
        "strategy_payload": payload,
        "data_contract": {"requirements": [{"dataset": "etf.share"}]},
        "execution_policy": {"policy_type": "FROZEN_RULE"},
        "research_claims": {"claim": "fixture"},
    }
    snapshot = {
        **snapshot_payload,
        "candidate_hash": canonical_sha256(snapshot_payload),
    }
    _write_json(package / "candidate_snapshot.json", snapshot)
    install_files = [
        "strategies/candidate_fixture.py",
        "charts/__init__.py",
        "charts/candidate_fixture.py",
    ]
    binding = {
        "schema_version": 1,
        "candidate_id": "S990-C001",
        "source_files": list(source_files),
        "implementation_sha256": source_hash,
        "install_files": install_files,
        "charts": {
            "module": "strategy_runtime.charts.candidate_fixture",
            "qualname": "CandidateCharts",
            "contract_version": 1,
            "source_files": list(chart_files),
            "source_sha256": chart_hash,
        },
    }
    _write_json(package / "runtime_binding.json", binding)
    files = {
        path.relative_to(package).as_posix(): _hash(path)
        for path in sorted(package.rglob("*"))
        if path.is_file()
    }
    identity = {
        "schema_version": 1,
        "candidate_snapshot": "candidate_snapshot.json",
        "runtime_binding": "runtime_binding.json",
        "runtime_root": "runtime/strategy_runtime",
        "files": files,
    }
    _write_json(
        package / "candidate_submission.json",
        {**identity, "package_hash": canonical_sha256(identity)},
    )
    return package


def test_candidate_package_locks_runtime_and_both_chart_modes(tmp_path: Path) -> None:
    path = _candidate_package(tmp_path)

    package = load_candidate_package(path)

    assert package.candidate_reference == "S990-C001"
    assert package.runtime_binding["charts"]["qualname"] == "CandidateCharts"
    source = package.runtime_root / "strategies" / "candidate_fixture.py"
    source.write_bytes(source.read_bytes() + b"\n# drift\n")
    with pytest.raises(ValueError, match="file hash differs"):
        load_candidate_package(path)


def test_candidate_review_imports_and_seals_researcher_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "czsc_trader").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0.1'\n", encoding="utf-8"
    )
    package_path = _candidate_package(repo)
    mandate_payload = {
        "schema_version": 1,
        "mandate_id": "EM-S990-C001-001",
        "strategy_id": "S990",
        "candidate_id": "C001",
        "development_cutoff": "2026-09-20",
        "forward_start": "2026-09-21",
        "evaluation_windows": {"full": {"start": "2025-01-01", "end": "2026-09-20"}},
        "benchmark": {"type": "strategy", "id": "BuyHold"},
        "objectives": [{"metric": "annual_return", "operator": ">=", "value": 0.0}],
        "cost_policy": {"mode": "FIXED"},
        "frequency_policy": {"mode": "OBSERVE"},
        "audit_requirements": {"runtime_acceptance": {"required_status": "PASS"}},
        "required_audits": ["runtime_acceptance"],
        "evidence_seen_through": "2026-09-20",
        "finalized_at": "2026-09-20T12:00:00+08:00",
        "finalized_by": "investment-director",
    }
    mandate_path = _write_json(
        repo / "research" / "S990" / "mandates" / "S990-C001.json",
        {**mandate_payload, "mandate_hash": canonical_sha256(mandate_payload)},
    )
    context = RepositoryContext.discover(repo)
    create_research_batch(
        context,
        _write_json(
            repo / "research-registration.json",
            {
                "strategy_id": "S990",
                "name": "候选包测试策略",
                "scope": ["588080.SH"],
                "research_intent": {"objective": "验证候选受理"},
                "credential_id": "SGC-S990-001",
            },
        ),
        actor="researcher",
        reason="批准测试研究批次",
    )
    captured: dict[str, object] = {}

    def reject_open_freeze_review(_context, **_kwargs):
        raise ValidationError("forced_review_failure", "forced")

    monkeypatch.setattr(
        candidate_service, "open_freeze_review", reject_open_freeze_review
    )
    with pytest.raises(ValidationError, match="forced"):
        review_candidate(context, package_path, mandate_path)
    assert not (repo / "strategies" / "S990").exists()

    def fake_open_freeze_review(_context, **kwargs) -> CommandResult:
        captured.update(kwargs)
        return CommandResult("PASS", "strategy.review.open", artifacts={"review": "sealed"})

    monkeypatch.setattr(candidate_service, "open_freeze_review", fake_open_freeze_review)

    result = review_candidate(context, package_path, mandate_path)

    governed = repo / "strategies" / "S990" / "candidates" / "S990-C001"
    assert result.result["candidate_state"] == "REVIEWED"
    assert result.artifacts == {"review": "sealed"}
    assert load_candidate_package(governed / "package").package_hash == result.result["package_hash"]
    assert captured["runtime_root"] == governed / "package" / "runtime" / "strategy_runtime"
    assert captured["actor"] == "investment-director"
    assert json.loads((governed / "review.json").read_text(encoding="utf-8"))[
        "credential_id"
    ] == "SGC-S990-001"


def _release_package(repo: Path, reference: str) -> None:
    strategy_id, version = reference.rsplit("-", 1)
    destination = repo / "strategies" / strategy_id / "releases" / version
    source = ROOT / "strategies" / strategy_id / "releases" / version
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def test_strategy_commands_cover_only_installed_srt_versions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "czsc_trader").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1'\n", encoding="utf-8")
    shutil.copytree(ROOT / "strategies", repo / "strategies")
    shutil.rmtree(repo / "strategies" / "deployments")
    _release_package(repo, "S007-v1")
    context = RepositoryContext.discover(repo)

    assert list_installed_strategies(context).result["strategies"] == []
    untracked = repo / "strategies" / "S007" / "releases" / "v1" / "untracked.py"
    untracked.write_text("raise RuntimeError('must not be deployed')\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="untracked or missing files"):
        deploy_strategy(context, "S007-v1")
    untracked.unlink()
    deployed = deploy_strategy(context, "S007-v1")
    listed = list_installed_strategies(context, "S007")
    info = strategy_info(context, "S007-v1")

    assert deployed.result["deployment_state"] == "SRT_DEPLOYED"
    assert [item["strategy_version_id"] for item in listed.result["strategies"]] == ["S007-v1"]
    assert info.result["strategy_version_id"] == "S007-v1"
    assert info.result["chart_contract"] is True
    assert not list(
        (repo / "strategies" / "S007" / "releases" / "v1").rglob("__pycache__")
    )


def test_investment_director_cli_uses_candidate_and_srt_namespaces() -> None:
    parser = build_parser()
    commands = (
        ["candidate", "review", "--package", "candidate", "--mandate", "mandate.json"],
        ["candidate", "evaluate", "S007-C002"],
        ["candidate", "freeze", "S007-C002", "--change-summary", "第二版"],
        ["strategy", "list", "S007"],
        ["strategy", "info", "S007-v1"],
        ["strategy", "deploy", "S007-v1"],
    )

    assert [parser.parse_args(args).command_name for args in commands] == [
        "candidate.review",
        "candidate.evaluate",
        "candidate.freeze",
        "strategy.list",
        "strategy.info",
        "strategy.deploy",
    ]
