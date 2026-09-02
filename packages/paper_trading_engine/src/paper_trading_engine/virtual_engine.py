"""Independent virtual-account decision and settlement engine."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
import csv
import json
from pathlib import Path
import math

from .store import PaperStore
from .virtual_fill import settle_limit_order


class VirtualAccountEngine:
    def __init__(self, store: PaperStore, advice) -> None:
        self.store = store
        self.advice = advice

    def refresh_account(self, account_id: str, session: date, bar: dict[str, object] | None):
        account = self.store.virtual_account(account_id)
        if bar is not None:
            for order in self.store.virtual_orders(account_id):
                if order["status"] != "PENDING" or order["valid_session"] != session.isoformat():
                    continue
                outcome = settle_limit_order(
                    order["side"], int(order["quantity"]), Decimal(order["limit_price"]),
                    Decimal(str(bar["open"])), Decimal(str(bar["high"])), Decimal(str(bar["low"])),
                )
                if outcome.status == "FILLED":
                    order_payload = json.loads(order["payload"])
                    self.store.settle_virtual_order(
                        account_id, order["order_id"], session.isoformat(), outcome.price,
                        Decimal(str(order_payload.get("fee_rate", "0.0005"))),
                    )
                else:
                    self.store.set_virtual_order_status(order["order_id"], outcome.status)
            account = self.store.virtual_account(account_id)
        if not bool(account["paused"]):
            decision = self.advice.get_decision(
                int(account["quantity"]), float(account["cash"]),
                cycle_target_quantity=account["cycle_target"],
                baseline=account["baseline_version"],
            )
            if decision.baseline != {
                "version": account["baseline_version"], "sha256": account["baseline_sha256"]
            }:
                raise ValueError("advice baseline identity differs from virtual account")
            self.store.update_virtual_account(
                account_id, cash=Decimal(account["cash"]), quantity=int(account["quantity"]),
                cycle_target=decision.cycle_target_quantity or None,
            )
            for index, order in enumerate(decision.orders):
                order_id = f"VO-{account_id}-{decision.decision_id}-{index}"
                payload = asdict(order)
                payload["fee_rate"] = decision.fee_rate
                self.store.save_virtual_order(
                    account_id, decision.decision_id, decision.valid_session.isoformat(), order_id, payload
                )
        if bar is not None:
            account = self.store.virtual_account(account_id)
            close = Decimal(str(bar["close"]))
            total_assets = Decimal(account["cash"]) + close * int(account["quantity"])
            self.store.save_virtual_snapshot(account_id, session.isoformat(), {
                "close": str(close), "cash": account["cash"], "quantity": account["quantity"],
                "total_assets": str(total_assets.quantize(Decimal("0.0001"))),
            })
        result = self.status(account_id)
        result["bar"] = bar
        return result

    def status(self, account_id: str):
        account = self.store.virtual_account(account_id)
        snapshots = self.store.virtual_snapshots(account_id)
        metrics = self._metrics(account, snapshots)
        return {
            **account,
            "orders": self.store.virtual_orders(account_id),
            "fills": self.store.virtual_fills(account_id),
            "snapshots": snapshots,
            "metrics": metrics,
        }

    def metrics(self, account_id: str, start: str | None = None, end: str | None = None):
        account = self.store.virtual_account(account_id)
        snapshots = self.store.virtual_snapshots(account_id)
        return self._metrics(account, snapshots, start=start, end=end)

    def _metrics(self, account, snapshots, *, start=None, end=None):
        snapshots = [
            item for item in snapshots
            if (start is None or item["session"] >= start) and (end is None or item["session"] <= end)
        ]
        initial = float(snapshots[0]["total_assets"]) if start is not None and snapshots else float(account["initial_cash"])
        values = [float(item["total_assets"]) for item in snapshots]
        ending = values[-1] if values else float(account["cash"])
        total_return = ending / initial - 1 if initial else None
        peak = 0.0
        max_drawdown = 0.0
        for value in values:
            peak = max(peak, value)
            if peak:
                max_drawdown = min(max_drawdown, value / peak - 1)
        years = max(1 / 252, len(values) / 252)
        annual_return = (ending / initial) ** (1 / years) - 1 if initial and ending > 0 else None
        calmar = None if not max_drawdown or annual_return is None else annual_return / abs(max_drawdown)
        pnl = [
            float(fill["realized_pnl"]) for fill in self.store.virtual_fills(account["account_id"])
            if fill["side"] == "SELL" and (start is None or fill["session"] >= start)
            and (end is None or fill["session"] <= end)
        ]
        wins = sum(value for value in pnl if value > 0)
        losses = -sum(value for value in pnl if value < 0)
        ratio = wins / losses if losses else None
        return {
            "observation_start": snapshots[0]["session"] if snapshots else None,
            "observation_end": snapshots[-1]["session"] if snapshots else None,
            "total_return": total_return,
            "maximum_drawdown": max_drawdown,
            "calmar_ratio": calmar if calmar is None or math.isfinite(calmar) else None,
            "win_loss_ratio": ratio,
            "closed_trades": len(pnl),
        }

    def refresh_all(self, session: date, bar: dict[str, object] | None):
        results = []
        for account in self.store.virtual_accounts():
            try:
                results.append(self.refresh_account(account["account_id"], session, bar))
            except Exception as exc:
                self.store.add_event("VIRTUAL_ACCOUNT_FAILED", {"account_id": account["account_id"], "error": str(exc)})
        return results

    def refresh_from_data(self, session: date, data_dir: Path, symbol: str = "588080.SH"):
        code = symbol.split(".", 1)[0]
        path = Path(data_dir) / f"{code}_execution_daily_{session.year}.csv"
        bar = None
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    if row.get("date") == session.isoformat():
                        bar = {
                            key: float(row[key])
                            for key in ("open", "high", "low", "close", "volume")
                        }
                        break
        return self.refresh_all(session, bar)
