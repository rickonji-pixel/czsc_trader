import json
import shutil
from pathlib import Path

from czsc_trader.cli.main import main
from czsc_trader.application.results import CommandResult


REPO_ROOT = Path(__file__).resolve().parents[1]


def _payload(capsys):
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def _temporary_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (tmp_path / "src" / "czsc_trader").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "configs" / "strategies", tmp_path / "configs" / "strategies")
    return tmp_path


def test_strategy_show_uses_formal_identity(capsys):
    code = main(
        [
            "strategy",
            "show",
            "--strategy",
            "S001",
            "--version",
            "v1",
            "--repo-root",
            str(REPO_ROOT),
        ]
    )
    payload = _payload(capsys)

    assert code == 0
    assert payload["status"] == "PASS"
    assert payload["result"]["strategy_id"] == "S001"
    assert payload["result"]["name"] == "综合基线策略"
    assert payload["result"]["version"] == "v1"
    assert payload["result"]["qualification"] == "PAPER_READY"


def test_strategy_read_commands_expose_history_evidence_and_validation(capsys):
    expected = {
        "list": ("strategies", 1),
        "history": ("events", 2),
        "performance": ("RESEARCH_BACKTEST", 1),
        "validate": ("strategies", 1),
    }
    for action, (field, count) in expected.items():
        args = ["strategy", action]
        if action in {"history", "performance"}:
            args.extend(["--strategy", "S001"])
        args.extend(["--repo-root", str(REPO_ROOT)])
        assert main(args) == 0
        payload = _payload(capsys)
        if action == "performance":
            assert len(payload["result"]["phases"][field]) == count
        elif action == "validate":
            assert payload["result"][field] == count
        else:
            assert len(payload["result"][field]) == count


def test_strategy_retire_requires_a_reason_and_changes_only_qualification(tmp_path, capsys):
    repo = _temporary_repo(tmp_path)
    code = main(
        [
            "strategy",
            "retire",
            "--strategy",
            "S001",
            "--version",
            "v1",
            "--actor",
            "tester",
            "--reason",
            "停止新部署",
            "--repo-root",
            str(repo),
        ]
    )
    retired = _payload(capsys)

    assert code == 0
    assert retired["result"]["to_state"] == "RETIRED"
    assert main(
        [
            "strategy",
            "show",
            "--strategy",
            "S001",
            "--version",
            "v1",
            "--repo-root",
            str(repo),
        ]
    ) == 0
    shown = _payload(capsys)
    assert shown["result"]["qualification"] == "RETIRED"
    assert shown["result"]["release_hash"] == retired["result"]["release_hash"]


def test_legacy_baseline_show_points_to_formal_strategy(capsys):
    assert main(
        [
            "baseline",
            "show",
            "--version",
            "baseline_20260903",
            "--symbol",
            "588080.SH",
            "--repo-root",
            str(REPO_ROOT),
        ]
    ) == 0
    payload = _payload(capsys)

    assert payload["result"]["strategy_id"] == "S001"
    assert payload["result"]["strategy_version"] == "v1"
    assert len(payload["result"]["release_hash"]) == 64


def test_legacy_baseline_list_identifies_the_formal_strategy(capsys):
    assert main(
        ["baseline", "list", "--repo-root", str(REPO_ROOT)]
    ) == 0
    payload = _payload(capsys)
    active = next(
        row
        for row in payload["result"]["baselines"]
        if row["version"] == "baseline_20260903"
    )

    assert active["strategy_id"] == "S001"
    assert active["strategy_version"] == "v1"
    assert len(active["release_hash"]) == 64


def test_strategy_evidence_import_copies_verified_bundle_and_groups_phase(
    tmp_path, capsys
):
    repo = _temporary_repo(tmp_path)
    source = repo / "paper-forward.json"
    source_payload = {
        "schema_version": 1,
        "account": {"account_id": "s001-forward"},
        "snapshots": [
            {"session": "2026-09-03", "total_assets": "100000.0000"},
            {"session": "2026-09-04", "total_assets": "101000.0000"},
        ],
        "closed_trade_pnl": [1000.0],
    }
    from strategy_manager import canonical_sha256

    evidence = {
        "schema_version": 1,
        "evidence_id": "EVD-PTE-S001-V1-TEST",
        "strategy_id": "S001",
        "version": "v1",
        "release_hash": "ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62",
        "phase": "PAPER_FORWARD",
        "period_start": "2026-09-03",
        "period_end": "2026-09-04",
        "data_identity": {"account_id": "s001-forward"},
        "initial_capital": 100000.0,
        "fee_rate": 0.0005,
        "maximum_drawdown": 0.0,
        "calmar_ratio": 2.0,
        "win_loss_ratio": None,
        "win_loss_ratio_status": "NO_LOSSES",
        "total_return": 0.01,
        "sharpe_ratio": None,
        "closed_trades": 1,
        "source_path": "pte://virtual-account/s001-forward",
        "source_hash": canonical_sha256(source_payload),
        "recorded_at": "2026-09-04T19:00:00+08:00",
        "recorded_by": "tester",
    }
    source.write_text(
        json.dumps(
            {"bundle_schema_version": 1, "evidence": evidence, "source": source_payload},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert main(
        [
            "strategy",
            "evidence",
            "add",
            "--input",
            str(source),
            "--repo-root",
            str(repo),
        ]
    ) == 0
    imported = _payload(capsys)
    copied = repo / "configs" / "strategies" / "S001" / "evidence" / (
        evidence["evidence_id"] + ".json"
    )
    assert imported["result"]["evidence_id"] == evidence["evidence_id"]
    assert json.loads(copied.read_text(encoding="utf-8"))["source"] == source_payload

    assert main(
        [
            "strategy",
            "performance",
            "--strategy",
            "S001",
            "--version",
            "v1",
            "--repo-root",
            str(repo),
        ]
    ) == 0
    performance = _payload(capsys)
    assert len(performance["result"]["phases"]["RESEARCH_BACKTEST"]) == 1
    assert len(performance["result"]["phases"]["PAPER_FORWARD"]) == 1
    assert performance["result"]["phases"]["LIVE"] == []


def test_strategy_evaluate_dispatches_experiment(monkeypatch, capsys):
    monkeypatch.setattr(
        "czsc_trader.cli.strategy_commands.evaluate_experiment",
        lambda context, experiment: CommandResult("PASS", "strategy.evaluate", {"experiment": experiment}),
    )
    assert main(["strategy", "evaluate", "--experiment", "0903_TEST", "--repo-root", str(REPO_ROOT)]) == 0
    assert _payload(capsys)["result"]["experiment"] == "0903_TEST"
