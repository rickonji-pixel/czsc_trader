"""Observe authenticated SRT publications required by active PTE accounts."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json


class PublicationInboxError(RuntimeError):
    """An active account has no consistent authenticated SRT publication."""


def _publication_identity(prepared_datasets: list[object]) -> str:
    releases: dict[str, object] = {}
    for prepared in prepared_datasets:
        releases[prepared.strategy.reference_id] = {
            "release_hash": prepared.strategy.release_hash,
            "requested_cutoff": prepared.available_through.isoformat(),
            "inputs": dict(prepared.input_identities),
            "execution_prices": dict(prepared.price_identities),
        }
    encoded = json.dumps(
        releases, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class PublicationInbox:
    """Describe the complete SRT publications available to active PTE accounts."""

    def __init__(self, *, store, advice) -> None:
        self.store = store
        self.advice = advice

    def observe(self) -> dict[str, object]:
        required: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
        for account in self.store.strategy_virtual_accounts():
            if account.get("status") == "RETIRED":
                continue
            symbol = str(account["symbol"]).upper()
            asset = str(account["asset_type"])
            release_id = f"{account['strategy_id']}-{account['strategy_version']}"
            required.setdefault((symbol, asset), {})[release_id] = account
        if not required:
            raise PublicationInboxError("PTE has no active strategy accounts")

        instruments: list[dict[str, object]] = []
        publication_ids: list[str] = []
        observed_cutoffs: set[str] = set()
        for (symbol, asset), accounts in sorted(required.items()):
            prepared_datasets = []
            instrument_cutoffs: set[str] = set()
            for release_id, account in sorted(accounts.items()):
                try:
                    prepared = self.advice.prepared_data_for_account(
                        strategy_id=str(account["strategy_id"]),
                        strategy_version=str(account["strategy_version"]),
                        symbol=symbol,
                        asset=asset,
                    )
                except Exception as exc:
                    raise PublicationInboxError(
                        f"{symbol}/{release_id}: SRT publication is unavailable: {exc}"
                    ) from exc
                if prepared.strategy.reference_id != release_id:
                    raise PublicationInboxError(
                        f"{symbol}: SRT returned a different strategy publication"
                    )
                try:
                    cutoff = prepared.available_through.isoformat()
                except (TypeError, ValueError) as exc:
                    raise PublicationInboxError(
                        f"{symbol}/{release_id}: publication cutoff is invalid"
                    ) from exc
                prepared_datasets.append(prepared)
                instrument_cutoffs.add(cutoff)
            if len(instrument_cutoffs) != 1:
                raise PublicationInboxError(
                    f"{symbol}: active strategy publications have different cutoffs"
                )
            cutoff = next(iter(instrument_cutoffs))
            publication_id = _publication_identity(prepared_datasets)
            observed_cutoffs.add(cutoff)
            publication_ids.append(publication_id)
            instruments.append(
                {
                    "symbol": symbol,
                    "asset_type": asset,
                    "result": {
                        "data_cutoff": cutoff,
                        "publication_id": publication_id,
                    },
                }
            )
        if len(observed_cutoffs) != 1:
            raise PublicationInboxError(
                "active account publications have different data cutoffs"
            )
        return {
            "data_cutoff": next(iter(observed_cutoffs)),
            "instruments": instruments,
            "publication_ids": publication_ids,
        }
