"""Import PTE decisions into an isolated, non-executable shadow ledger."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from .store import LiveObservationStore


class ShadowImportError(RuntimeError):
    pass


def _read_only_connection(path: Path) -> sqlite3.Connection:
    source = path.resolve()
    if not source.is_file():
        raise ShadowImportError(f"PTE source database does not exist: {source}")
    connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def import_pte_shadow(
    store: LiveObservationStore,
    *,
    source_database: str | Path,
    source_account_id: str,
    symbol: str,
) -> dict[str, object]:
    """Mirror immutable PTE facts without creating anything executable."""
    source_path = Path(source_database)
    normalized_symbol = str(symbol).strip().upper()
    connection = _read_only_connection(source_path)
    try:
        account = connection.execute(
            "SELECT account_id,account_type,channel_id,symbol,asset_type,"
            "qualification_snapshot FROM virtual_accounts WHERE account_id=?",
            (source_account_id,),
        ).fetchone()
        if account is None:
            raise ShadowImportError(f"PTE source account does not exist: {source_account_id}")
        expected = {
            "account_type": "STRATEGY",
            "channel_id": "futu_simulate_us",
            "symbol": normalized_symbol,
            "asset_type": "stock",
        }
        actual = {key: account[key] for key in expected}
        if actual != expected:
            raise ShadowImportError(
                f"PTE source account is outside the approved US-stock shadow scope: {actual}"
            )
        if account["qualification_snapshot"] not in {"PAPER_READY", "LIVE_READY"}:
            raise ShadowImportError("PTE source strategy is not approved for observation")

        decision_rows = connection.execute(
            "SELECT decision_id,payload FROM decisions WHERE account_id=? "
            "ORDER BY signal_date,decision_id",
            (source_account_id,),
        ).fetchall()
        intent_rows = connection.execute(
            "SELECT intent_id,decision_id,order_sequence,channel_id,symbol,side,quantity,"
            "limit_price,valid_session,payload,status FROM intents WHERE account_id=? "
            "ORDER BY valid_session,decision_id,order_sequence",
            (source_account_id,),
        ).fetchall()
    finally:
        connection.close()

    imported_decisions = 0
    for row in decision_rows:
        payload = json.loads(row["payload"])
        payload["shadow_disposition"] = "SHADOW_ONLY"
        payload["source_account_id"] = source_account_id
        imported_decisions += int(
            store.save_shadow_decision(source_account_id, str(row["decision_id"]), payload)
        )

    imported_intents = 0
    for row in intent_rows:
        if row["channel_id"] != "futu_simulate_us" or row["symbol"] != normalized_symbol:
            raise ShadowImportError("PTE intent escaped the approved US-stock shadow scope")
        source_payload: dict[str, Any] = json.loads(row["payload"])
        payload = {
            "schema": "longbridge_shadow_intent.v1",
            "source_intent_id": str(row["intent_id"]),
            "source_account_id": source_account_id,
            "decision_id": str(row["decision_id"]),
            "order_sequence": int(row["order_sequence"]),
            "symbol": normalized_symbol,
            "side": str(row["side"]),
            "quantity": int(row["quantity"]),
            "order_type": str(source_payload.get("order_type", "LIMIT")),
            "reference_price": str(row["limit_price"]),
            "valid_session": str(row["valid_session"]),
            "source_status": str(row["status"]),
            "disposition": "SHADOW_ONLY",
        }
        imported_intents += int(store.save_shadow_intent(payload))

    run_id = store.record_import_run(
        source_database=source_path,
        source_account_id=source_account_id,
        imported_decisions=imported_decisions,
        imported_intents=imported_intents,
    )
    return {
        "run_id": run_id,
        "source_account_id": source_account_id,
        "source_qualification": str(account["qualification_snapshot"]),
        "decisions_seen": len(decision_rows),
        "intents_seen": len(intent_rows),
        "decisions_imported": imported_decisions,
        "intents_imported": imported_intents,
        "disposition": "SHADOW_ONLY",
        "order_write_capability": False,
    }
