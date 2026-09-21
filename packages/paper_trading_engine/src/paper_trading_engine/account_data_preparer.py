"""Account-scoped SRT data preparation for the PTE scheduler."""

from __future__ import annotations

from datetime import date, datetime


class AccountDataPreparationError(RuntimeError):
    """One account could not prepare authenticated strategy data."""


class AccountDataPreparer:
    """Delegate one account's data preparation to its SRT adapter."""

    def __init__(self, *, advice) -> None:
        self.advice = advice

    def latest_completed_signal_date(self, at: datetime) -> date:
        return self.advice.latest_completed_signal_date(at)

    def prepare(
        self, account: dict[str, object], *, signal_date: date
    ):
        account_id = str(account["account_id"])
        release_id = f"{account['strategy_id']}-{account['strategy_version']}"
        try:
            prepared = self.advice.prepare_account_data(
                account_id=account_id,
                strategy_id=str(account["strategy_id"]),
                strategy_version=str(account["strategy_version"]),
                symbol=str(account["symbol"]).upper(),
                asset=str(account["asset_type"]),
                signal_date=signal_date,
            )
        except Exception as exc:
            raise AccountDataPreparationError(
                f"{account_id}/{release_id}: SRT data preparation failed: {exc}"
            ) from exc
        if prepared is None:
            return None
        if prepared.strategy.reference_id != release_id:
            raise AccountDataPreparationError(
                f"{account_id}: SRT returned data for another strategy"
            )
        if prepared.available_through != signal_date:
            raise AccountDataPreparationError(
                f"{account_id}: SRT prepared through {prepared.available_through}, "
                f"expected {signal_date}"
            )
        return prepared
