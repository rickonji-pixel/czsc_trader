"""Observe SRT-prepared data required by active PTE accounts."""

from __future__ import annotations

from hashlib import sha256
import json


class PreparedDataInboxError(RuntimeError):
    """An active account has no consistent authenticated prepared data."""


class PreparedDataInbox:
    """Describe prepared SRT data available to active PTE accounts."""

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
            raise PreparedDataInboxError("PTE has no active strategy accounts")

        instruments: list[dict[str, object]] = []
        data_identities: list[str] = []
        observed_dates: set[str] = set()
        for (symbol, asset), accounts in sorted(required.items()):
            account_results = []
            instrument_dates: set[str] = set()
            for release_id, account in sorted(accounts.items()):
                try:
                    prepared = self.advice.prepare_for_account(
                        strategy_id=str(account["strategy_id"]),
                        strategy_version=str(account["strategy_version"]),
                        symbol=symbol,
                        asset=asset,
                    )
                except Exception as exc:
                    raise PreparedDataInboxError(
                        f"{symbol}/{release_id}: SRT prepared data is unavailable: {exc}"
                    ) from exc
                if prepared.strategy.reference_id != release_id:
                    raise PreparedDataInboxError(
                        f"{symbol}: SRT returned data for another strategy"
                    )
                prepared_through = prepared.available_through.isoformat()
                account_results.append(prepared)
                instrument_dates.add(prepared_through)
            if len(instrument_dates) != 1:
                raise PreparedDataInboxError(
                    f"{symbol}: active strategies have different prepared-through dates"
                )
            prepared_through = next(iter(instrument_dates))
            instrument_identity = sha256(
                json.dumps(
                    sorted(item.data_identity for item in account_results),
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            observed_dates.add(prepared_through)
            data_identities.append(instrument_identity)
            instruments.append(
                {
                    "symbol": symbol,
                    "asset_type": asset,
                    "result": {
                        "prepared_through": prepared_through,
                        "data_identity": instrument_identity,
                    },
                }
            )
        if len(observed_dates) != 1:
            raise PreparedDataInboxError(
                "active account data has different prepared-through dates"
            )
        return {
            "prepared_through": next(iter(observed_dates)),
            "instruments": instruments,
            "data_identities": data_identities,
        }
