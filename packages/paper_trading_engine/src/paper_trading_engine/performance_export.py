from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import uuid


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calculate_metrics(
    initial: float, snapshots: list[dict], pnl: list[float],
) -> dict[str, object]:
    values = [initial, *[float(item["total_assets"]) for item in snapshots]]
    ending = values[-1]
    total_return = ending / initial - 1
    peak = values[0]
    maximum_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        maximum_drawdown = min(maximum_drawdown, value / peak - 1)
    years = max(1 / 252, len(snapshots) / 252)
    annualized = (ending / initial) ** (1 / years) - 1 if ending > 0 else None
    calmar = (
        None
        if maximum_drawdown == 0 or annualized is None
        else annualized / abs(maximum_drawdown)
    )
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
    if len(returns) > 1 and statistics.stdev(returns) > 0:
        sharpe = statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(252)
    else:
        sharpe = None
    wins = sum(value for value in pnl if value > 0)
    losses = -sum(value for value in pnl if value < 0)
    if not pnl:
        ratio, ratio_status = None, "NO_CLOSED_TRADES"
    elif wins == 0:
        ratio, ratio_status = None, "NO_WINS"
    elif losses == 0:
        ratio, ratio_status = None, "NO_LOSSES"
    else:
        ratio, ratio_status = wins / losses, "VALID"
    return {
        "maximum_drawdown": maximum_drawdown,
        "calmar_ratio": calmar,
        "win_loss_ratio": ratio,
        "win_loss_ratio_status": ratio_status,
        "total_return": total_return,
        "sharpe_ratio": sharpe,
        "closed_trades": len(pnl),
    }


def closed_trade_pnl(fills: list[dict]) -> list[float]:
    """Aggregate partial SELL fills into one realized result per channel order."""
    by_order: dict[str, float] = {}
    for fill in fills:
        if fill.get("side") != "SELL":
            continue
        order_id = str(fill["order_id"])
        by_order[order_id] = by_order.get(order_id, 0.0) + float(fill["realized_pnl"])
    return list(by_order.values())


def export_performance(
    store,
    account_id: str,
    output: Path,
    *,
    recorded_by: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, object]:
    account = store.virtual_account(account_id)
    identity_fields = (
        "strategy_id",
        "strategy_name_snapshot",
        "strategy_version",
        "release_hash",
        "qualification_snapshot",
    )
    if any(not account.get(field) for field in identity_fields):
        raise ValueError("virtual account has no formal strategy identity")
    snapshots = [
        {"session": item["session"], "total_assets": item["total_assets"]}
        for item in store.account_snapshots(account_id)
        if (start is None or item["session"] >= start) and (end is None or item["session"] <= end)
    ]
    if not snapshots:
        raise ValueError("performance export requires at least one snapshot")
    fills = [
        item
        for item in store.account_fills(account_id)
        if item["side"] == "SELL"
        and (start is None or item["occurred_at"][:10] >= start)
        and (end is None or item["occurred_at"][:10] <= end)
    ]
    pnl = closed_trade_pnl(fills)
    intents = store.account_intents(account_id)
    fee_rates = {
        float(item["payload"]["fee_rate"])
        for item in intents
        if "fee_rate" in item["payload"]
    }
    if len(fee_rates) != 1:
        raise ValueError("performance export requires one unambiguous fee rate")
    source = {
        "schema_version": 1,
        "account": {
            "account_id": account_id,
            "strategy_id": account["strategy_id"],
            "strategy_version": account["strategy_version"],
            "release_hash": account["release_hash"],
            "initial_cash": account["initial_cash"],
        },
        "snapshots": snapshots,
        "closed_trade_pnl": pnl,
    }
    source_hash = _canonical_sha256(source)
    metrics = calculate_metrics(float(account["initial_cash"]), snapshots, pnl)
    recorded_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    evidence_id = f"EVD-PTE-{uuid.uuid4().hex.upper()}"
    evidence = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "strategy_id": account["strategy_id"],
        "version": account["strategy_version"],
        "release_hash": account["release_hash"],
        "phase": "PAPER_FORWARD",
        "period_start": snapshots[0]["session"],
        "period_end": snapshots[-1]["session"],
        "data_identity": {
            "account_id": account_id,
            "account_created_at": account["created_at"],
            "observation_start": account["observation_start"],
        },
        "initial_capital": float(account["initial_cash"]),
        "fee_rate": fee_rates.pop(),
        **metrics,
        "source_path": f"pte://virtual-account/{account_id}",
        "source_hash": source_hash,
        "recorded_at": recorded_at,
        "recorded_by": str(recorded_by).strip(),
    }
    if not evidence["recorded_by"]:
        raise ValueError("recorded_by is required")
    bundle = {"bundle_schema_version": 1, "evidence": evidence, "source": source}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)
    return bundle
