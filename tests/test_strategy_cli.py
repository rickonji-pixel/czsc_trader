import json
import shutil
from pathlib import Path

from czsc_trader.cli.main import main


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
