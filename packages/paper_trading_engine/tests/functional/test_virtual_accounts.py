from argparse import Namespace
from dataclasses import replace
from datetime import date
import json
import hashlib
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest

from paper_trading_engine.store import PaperStore
from paper_trading_engine.account_engine import AccountEngine
from paper_trading_engine import cli as pte_cli
from paper_trading_engine.web_api import PteWebApi
from pte_support import decision


def create_account(store, account_id, version, marker):
    return store.create_virtual_account(
        account_id, f"{account_id}模拟账户", "legacy", marker * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version=version, release_hash=marker * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
    )


def test_ft_pte01_account_model_migration_and_independent_futu_ledgers(tmp_path):
    store = PaperStore(tmp_path / "account-centric.db")
    create_account(store, "s001-v1", "v1", "a")
    create_account(store, "s001-v2", "v2", "b")
    assert {row["channel_id"] for row in store.virtual_accounts()} == {"futu"}
    assert {row["selection_data_cutoff"] for row in store.virtual_accounts()} == {"2026-09-02"}
    assert len(store.query_audit_events(event_type="ACCOUNT_STRATEGY_BOUND")) == 2
    assert len(store.query_audit_events(event_type="ACCOUNT_CHANNEL_BOUND", channel="futu")) == 2
    tables = {row[0] for row in store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert not {"virtual_intents", "virtual_orders", "virtual_fills", "virtual_snapshots"} & tables

    intent = store.create_account_intent(
        account_id="s001-v2", decision_id="DEC-2", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04", fee_rate="0.0005",
    )
    assert store.claim_account_intent(intent["intent_id"])
    store.bind_channel_order(intent["intent_id"], "1001", {
        "channel_order_id": "1001", "symbol": "588080.SH", "side": "BUY",
        "quantity": 1000, "limit_price": 1.68, "status": "SUBMITTED",
        "cumulative_filled_quantity": 0, "average_fill_price": 0,
        "remark": intent["intent_id"],
    })
    store.apply_fill_increment(
        "1001", cumulative_quantity=1000, average_price="1.670",
        occurred_at="2026-09-04T01:31:00+00:00",
    )
    assert store.virtual_account("s001-v1")["quantity"] == 0
    assert store.virtual_account("s001-v2")["quantity"] == 1000
    assert store.virtual_account("s001-v2")["cash"] == "98329.1650"
    assert store.virtual_account("s001-v2")["total_assets"] == "99999.1650"
    assert store.account_fills("s001-v2")[0]["channel_id"] == "futu"

    sell_intent = store.create_account_intent(
        account_id="s001-v2", decision_id="DEC-3", order_sequence=0,
        symbol="588080.SH", side="SELL", quantity=1000,
        limit_price="1.650", valid_session="2026-09-04", fee_rate="0.0005",
        order_type="MARKET",
    )
    assert store.claim_account_intent(sell_intent["intent_id"])
    store.bind_channel_order(sell_intent["intent_id"], "1002", {
        "channel_order_id": "1002", "symbol": "588080.SH", "side": "SELL",
        "quantity": 1000, "limit_price": 1.65, "status": "SUBMITTED",
        "cumulative_filled_quantity": 0, "average_fill_price": 0,
        "remark": sell_intent["intent_id"], "order_type": "MARKET",
    })
    store.apply_fill_increment(
        "1002", cumulative_quantity=1000, average_price="1.600",
        occurred_at="2026-09-04T01:32:00+00:00",
    )
    assert store.virtual_account("s001-v2")["quantity"] == 0
    assert store.virtual_account("s001-v2")["cash"] == "99928.3650"
    assert store.virtual_account("s001-v2")["total_assets"] == "99928.3650"
    assert store.account_fills("s001-v2")[0]["price"] == "1.600"
    store.close()


def test_strategy_name_sync_is_release_guarded_idempotent_and_audited(tmp_path):
    store = PaperStore(tmp_path / "strategy-name.db")
    create_account(store, "s001-v1", "v1", "a")

    assert store.synchronize_account_strategy_name(
        "s001-v1", "a" * 64, "科创50多因子趋势策略"
    )
    assert not store.synchronize_account_strategy_name(
        "s001-v1", "a" * 64, "科创50多因子趋势策略"
    )
    assert store.virtual_account("s001-v1")["strategy_name_snapshot"] == (
        "科创50多因子趋势策略"
    )
    events = store.query_audit_events(event_type="ACCOUNT_STRATEGY_NAME_UPDATED")
    assert len(events) == 1
    assert events[0]["details"] == {
        "previous_name": "综合基线策略",
        "name": "科创50多因子趋势策略",
    }
    with pytest.raises(ValueError, match="release hash differs"):
        store.synchronize_account_strategy_name(
            "s001-v1", "b" * 64, "不应写入"
        )
    store.close()


def test_account_metrics_use_prior_snapshot_as_window_baseline(tmp_path):
    store = PaperStore(tmp_path / "window-metrics.db")
    create_account(store, "s001-v2", "v2", "b")
    store.save_account_snapshot("s001-v2", "2026-09-09", {
        "quantity": 0, "total_assets": "102000.0000",
    })
    store.save_account_snapshot("s001-v2", "2026-09-10", {
        "quantity": 0, "total_assets": "100980.0000",
    })

    metrics = AccountEngine(store, advice=None).metrics(
        "s001-v2", start="2026-09-10", end="2026-09-10",
    )

    assert metrics["baseline_assets"] == 102000
    assert metrics["total_return"] == pytest.approx(-0.01)
    assert metrics["calmar_ratio"] is None
    assert metrics["annualization_status"] == "INSUFFICIENT_OBSERVATIONS"
    store.close()


def test_account_comparison_uses_each_accounts_observation_window(tmp_path):
    store = PaperStore(tmp_path / "comparison-metrics.db")
    create_account(store, "s001-v1", "v1", "a")
    create_account(store, "s001-v2", "v2", "b")
    store.save_account_snapshot("s001-v1", "2026-09-03", {
        "quantity": 0, "total_assets": "100000.0000",
    })
    store.save_account_snapshot("s001-v1", "2026-09-04", {
        "quantity": 0, "total_assets": "90000.0000",
    })
    store.save_account_snapshot("s001-v2", "2026-09-04", {
        "quantity": 0, "total_assets": "110000.0000",
    })
    virtual = AccountEngine(store, advice=None)
    api = PteWebApi(SimpleNamespace(store=store, virtual=virtual, channel=None))

    result = api.comparison([])

    assert result["metric_basis"] == "ACCOUNT_OBSERVATION_WINDOW"
    assert "common_window" not in result
    by_account = {row["account_id"]: row["metrics"] for row in result["accounts"]}
    assert by_account["s001-v1"]["observation_start"] == "2026-09-03"
    assert by_account["s001-v1"]["observation_count"] == 2
    assert by_account["s001-v1"]["total_return"] == pytest.approx(-0.10)
    assert by_account["s001-v2"]["observation_start"] == "2026-09-04"
    assert by_account["s001-v2"]["observation_count"] == 1
    assert by_account["s001-v2"]["total_return"] == pytest.approx(0.10)
    store.close()


def test_ft_pte03_account_chart_builds_bounded_scope_and_reuses_cache(tmp_path, monkeypatch):
    from paper_trading_engine import account_chart
    from paper_trading_engine.account_chart import AccountChartService

    store = PaperStore(tmp_path / "chart.db")
    create_account(store, "s001-v2", "v2", "b")
    store.save_account_decision(
        "s001-v2",
        {
            "account_id": "s001-v2",
            "decision_id": "DEC-1",
            "signal_date": "2026-09-03",
            "valid_session": "2026-09-04",
            "action": "WAIT",
            "target_quantity": 0,
        },
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dates = list(pd.bdate_range(end="2026-09-02", periods=185)) + list(
        pd.bdate_range("2026-09-03", "2026-09-04")
    )
    rows = ["date,open,high,low,close,volume,amount"]
    for index, dt in enumerate(dates):
        close = 1 + index / 1000
        rows.append(f"{dt.date().isoformat()},{close},{close + .01},{close - .01},{close},1,1")
    csv_bytes = ("\n".join(rows) + "\n").encode()
    data_file = data_dir / "588080_daily_2026.csv"
    data_file.write_bytes(csv_bytes)
    manifest = {
        "symbol": "588080.SH",
        "files": {
            data_file.name: {
                "frequency": "daily",
                "sha256": hashlib.sha256(csv_bytes).hexdigest(),
            }
        },
        "fetch_metadata": {"daily": {"adjustment": "hfq"}},
    }
    (data_dir / "588080_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    calls = []

    def renderer(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "<html>chart</html>", "")

    service = AccountChartService(
        store,
        data_dir=data_dir,
        cache_dir=tmp_path / "charts",
        trader_executable="czsc-trader",
        runner=renderer,
    )
    first = service.status("s001-v2")
    assert first["status"] == "READY"
    assert first["scope"] == {"account_id": "s001-v2", "release_id": "S001-v2"}
    assert service.chart_path("s001-v2").read_text(encoding="utf-8") == "<html>chart</html>"
    request = json.loads(calls[0][1]["input"])
    assert len(request["market_data"]["bars"]) == 62
    assert request["context_sessions"] == 60
    assert request["market_data"]["bars"][0]["date"] == dates[125].date().isoformat()
    assert request["market_data"]["bars"][-1]["date"] == "2026-09-04"
    assert {row["account_id"] for row in request["decisions"]} == {"s001-v2"}
    assert request["orders"] == []
    assert set(request["decisions"][0]) == {
        "account_id", "decision_id", "signal_date", "valid_session", "generated_at",
        "action", "target_quantity", "factor_score", "regime",
    }
    assert calls[0][0] == [
        "czsc-trader",
        "chart",
        "observation",
        "--format",
        "html",
        "--plotly-runtime",
        "external",
    ]

    second = service.status("s001-v2")
    assert second["fingerprint"] == first["fingerprint"]
    assert len(calls) == 1

    monkeypatch.setattr(
        store,
        "account_orders",
        lambda _account_id: [
            {
                "account_id": "s001-v2",
                "created_at": "2026-09-04T09:30:00+08:00",
                "updated_at": "2026-09-04T09:30:05+08:00",
                "status": "FILLED_ALL",
            }
        ],
    )
    order_refresh = service.status("s001-v2")
    assert order_refresh["fingerprint"] == first["fingerprint"]
    assert len(calls) == 1

    monkeypatch.setattr(account_chart, "CACHE_RENDER_REVISION", "next-layout")
    refreshed = service.status("s001-v2")
    assert refreshed["fingerprint"] != first["fingerprint"]
    assert len(calls) == 2
    store.close()

    legacy = tmp_path / "unsafe-legacy.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,event_type TEXT NOT NULL,payload TEXT NOT NULL);"
        "CREATE TABLE intents(intent_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,status TEXT NOT NULL,channel_order_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
        "CREATE TABLE orders(channel_order_id TEXT PRIMARY KEY,payload TEXT NOT NULL,cumulative_filled_quantity INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
    )
    connection.execute(
        "INSERT INTO intents VALUES(?,?,?,?,?,?,?)",
        ("OLD", "DEC", json.dumps({}), "PENDING_SUBMIT", None, "x", "x"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="requires empty legacy trading tables"):
        PaperStore(legacy)

    safe_legacy = tmp_path / "safe-legacy.db"
    connection = sqlite3.connect(safe_legacy)
    connection.executescript(
        "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,event_type TEXT NOT NULL,payload TEXT NOT NULL);"
        "CREATE TABLE intents(intent_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,status TEXT NOT NULL,channel_order_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
        "CREATE TABLE orders(channel_order_id TEXT PRIMARY KEY,payload TEXT NOT NULL,cumulative_filled_quantity INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
    )
    connection.close()
    migrated = PaperStore(safe_legacy)
    assert migrated.get_setting("account_execution_schema") == "account_execution.v1"
    assert migrated.query_audit_events(event_type="ACCOUNT_EXECUTION_MIGRATED")
    migrated.close()


def test_ft_pte02_selection_cutoff_is_required_immutable_and_safely_backfilled(tmp_path):
    store = PaperStore(tmp_path / "cutoff.db")
    columns = {
        row[1] for row in store._connection.execute("PRAGMA table_info(virtual_accounts)")
    }
    assert "selection_data_cutoff" in columns

    with pytest.raises(ValueError, match="selection_data_cutoff"):
        store.create_virtual_account(
            "missing", "缺少截止日", "legacy", "c" * 64, 100_000,
            strategy_id="S001", strategy_name_snapshot="综合基线策略",
            strategy_version="v1", release_hash="c" * 64,
            qualification_snapshot="PAPER_READY", selection_data_cutoff="",
        )

    account = create_account(store, "s001-v1", "v1", "a")
    assert account["selection_data_cutoff"] == "2026-09-02"
    with store._connection:
        store._connection.execute(
            "UPDATE virtual_accounts SET selection_data_cutoff=NULL WHERE account_id='s001-v1'"
        )
    assert store.backfill_account_selection_cutoff("s001-v1", "x" * 64, "2026-08-28") is False
    assert store.virtual_account("s001-v1")["selection_data_cutoff"] is None
    assert store.backfill_account_selection_cutoff("s001-v1", "a" * 64, "2026-08-28") is True
    assert store.virtual_account("s001-v1")["selection_data_cutoff"] == "2026-08-28"
    assert store.backfill_account_selection_cutoff("s001-v1", "a" * 64, "2026-09-01") is False
    assert store.virtual_account("s001-v1")["selection_data_cutoff"] == "2026-08-28"
    store.close()


def test_ft_pte02_new_account_is_created_only_after_strategy_runtime_preflight(
    tmp_path, monkeypatch,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "588080_manifest.json").write_text(
        json.dumps({
            "symbol": "588080.SH",
            "asset_type": "etf",
            "requested_end": "2026-09-16",
            "files": {
                "588080_daily_2026.csv": {
                    "frequency": "daily",
                    "last": "2026-09-15T00:00:00",
                },
            },
        }),
        encoding="utf-8",
    )
    release_hash = "7" * 64
    identity = {
        "strategy_id": "S007",
        "name": "多源机会风险门控",
        "version": "v1",
        "release_id": "S007-v1",
        "release_hash": release_hash,
        "qualification": "PAPER_READY",
        "selection_data_cutoff": "2026-09-02",
        "strategy_payload": {
            "rule": {"execution": {"capital": {"fee_rate": 0.001}}},
        },
    }
    args = Namespace(
        account_action="create",
        database=tmp_path / "runtime.db",
        data_dir=data_dir,
        repo_root=tmp_path,
        advice_executable=Path("czsc-trader"),
        account_id="s007-v1",
        name="S007-v1模拟账户",
        strategy="S007",
        baseline=None,
        strategy_version="v1",
        symbol="588080.SH",
        asset="etf",
        initial_cash="100000",
    )
    monkeypatch.setattr(pte_cli, "_validate_strategy", lambda _args: identity)
    support_calls = []

    def fail_support(_self, strategy_id, strategy_version, cutoff):
        support_calls.append((strategy_id, strategy_version, cutoff))
        raise RuntimeError("support unavailable")

    monkeypatch.setattr(
        pte_cli.AccountDataPublisher, "publish_strategy_support", fail_support,
    )
    with pytest.raises(RuntimeError, match="support unavailable"):
        pte_cli._run_account_command(args)
    assert support_calls == [("S007", "v1", "2026-09-15")]
    empty = PaperStore(args.database)
    assert empty.virtual_accounts() == []
    empty.close()

    monkeypatch.setattr(
        pte_cli.AccountDataPublisher,
        "publish_strategy_support",
        lambda _self, strategy_id, strategy_version, cutoff: {
            "release_id": f"{strategy_id}-{strategy_version}",
            "data_cutoff": cutoff,
            "support_required": True,
        },
    )
    accepted = replace(
        decision(),
        signal_date=date(2026, 9, 15),
        valid_session=date(2026, 9, 16),
        data_cutoff=date(2026, 9, 15),
        strategy={
            "strategy_id": "S007",
            "name": "多源机会风险门控",
            "version": "v1",
            "release_id": "S007-v1",
            "release_hash": release_hash,
            "qualification": "PAPER_READY",
        },
        fee_rate=0.001,
    )
    monkeypatch.setattr(
        pte_cli.CliAdviceClient,
        "get_decision",
        lambda _self, *_args, **_kwargs: accepted,
    )
    created = pte_cli._run_account_command(args)
    assert created["account_id"] == "s007-v1"
    assert created["strategy_id"] == "S007"
    assert created["release_hash"] == release_hash
    assert created["initial_cash"] == "100000.0000"
