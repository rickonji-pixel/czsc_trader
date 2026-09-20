"""Observe authenticated SRT publications required by active PTE accounts."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json


class PublicationInboxError(RuntimeError):
    """An active account has no consistent authenticated SRT publication."""


def _publication_identity(contexts: list[object]) -> str:
    releases: dict[str, object] = {}
    for context in contexts:
        publication = context.strategy_data
        inputs: dict[str, str] = {}
        for name, result in sorted(publication.input_results.items()):
            identity = result.identity
            if identity is None or not identity.content_sha256:
                raise PublicationInboxError(
                    f"{publication.release_id}: publication input identity is incomplete"
                )
            inputs[name] = identity.content_sha256
        releases[publication.release_id] = {
            "release_hash": publication.release_hash,
            "requested_cutoff": publication.requested_cutoff,
            "inputs": inputs,
            "execution_prices": dict(context.pricing_data.identity_hashes),
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
            contexts = []
            instrument_cutoffs: set[str] = set()
            for release_id, account in sorted(accounts.items()):
                try:
                    context = self.advice.runtime_context_for_account(
                        strategy_id=str(account["strategy_id"]),
                        strategy_version=str(account["strategy_version"]),
                        symbol=symbol,
                        asset=asset,
                    )
                except Exception as exc:
                    raise PublicationInboxError(
                        f"{symbol}/{release_id}: SRT publication is unavailable: {exc}"
                    ) from exc
                publication = context.strategy_data
                if publication.release_id != release_id:
                    raise PublicationInboxError(
                        f"{symbol}: SRT returned a different strategy publication"
                    )
                try:
                    cutoff = date.fromisoformat(
                        str(publication.requested_cutoff)
                    ).isoformat()
                except (TypeError, ValueError) as exc:
                    raise PublicationInboxError(
                        f"{symbol}/{release_id}: publication cutoff is invalid"
                    ) from exc
                contexts.append(context)
                instrument_cutoffs.add(cutoff)
            if len(instrument_cutoffs) != 1:
                raise PublicationInboxError(
                    f"{symbol}: active strategy publications have different cutoffs"
                )
            cutoff = next(iter(instrument_cutoffs))
            publication_id = _publication_identity(contexts)
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
