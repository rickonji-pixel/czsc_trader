import json
from decimal import Decimal
from pathlib import Path

from live_execution_gateway.cli import main
from live_execution_gateway.live_store import LiveTradeStore


RELEASE_HASH = "a" * 64


def _paper_ready_repo(path: Path) -> None:
    root = path / "strategies" / "S103"
    (root / "versions").mkdir(parents=True)
    (root / "versions" / "v1.json").write_text(json.dumps({
        "strategy_id": "S103", "version": "v1", "release_id": "S103-v1",
        "release_hash": RELEASE_HASH, "selection_data_cutoff": "2026-09-01",
    }), encoding="utf-8")
    (root / "lifecycle.jsonl").write_text(json.dumps({
        "strategy_id": "S103", "version": "v1", "release_hash": RELEASE_HASH,
        "to_state": "PAPER_READY",
    }) + "\n", encoding="utf-8")


def test_correct_arm_phrase_still_cannot_bypass_paper_ready(tmp_path: Path, capsys) -> None:
    _paper_ready_repo(tmp_path)
    database = tmp_path / "live.db"
    store = LiveTradeStore(database)
    try:
        store.register_deployment(
            account_id="live-1", strategy_id="S103", strategy_version="v1",
            release_hash=RELEASE_HASH, symbol="MU.US",
            max_order_notional=Decimal("1"), max_gross_notional=Decimal("1"),
            cash_reserve=Decimal("0"),
        )
        challenge = store.issue_arm_challenge("live-1")
    finally:
        store.close()

    code = main([
        "arm", "--repo-root", str(tmp_path), "--database", str(database),
        "--account-id", "live-1", "--challenge-id", challenge["challenge_id"],
        "--confirm", challenge["confirmation_phrase"], "--hours", "1",
        "--actor", "owner", "--reason", "test",
    ])
    payload = json.loads(capsys.readouterr().out)
    store = LiveTradeStore(database)
    try:
        deployment = store.deployment("live-1")
    finally:
        store.close()

    assert code == 1
    assert payload["error"]["type"] == "PermissionError"
    assert "PAPER_READY" in payload["error"]["message"]
    assert deployment["status"] == "DISARMED"
