"""Persistent, auditable strategy binding for the single Futu simulation channel."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re

from .audit import AuditRecorder


BINDING_KEY = "futu_strategy_binding"
TERMINAL_STATUSES = {
    "SUBMIT_FAILED", "FILLED_ALL", "CANCELLED_ALL", "FAILED", "DISABLED",
    "DELETED", "FILL_CANCELLED",
}


class ChannelBindingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChannelStrategyBinding:
    strategy_id: str
    strategy_version: str
    release_id: str
    release_hash: str
    qualification: str
    bound_at: str
    bound_by: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def load_channel_binding(store) -> ChannelStrategyBinding | None:
    raw = store.get_setting(BINDING_KEY)
    if not raw:
        return None
    try:
        return ChannelStrategyBinding(**json.loads(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ChannelBindingError("Futu策略绑定数据损坏") from exc


def _validate_release(release: dict[str, object]) -> dict[str, str]:
    normalized = {
        "strategy_id": str(release.get("strategy_id", "")),
        "strategy_version": str(release.get("strategy_version") or release.get("version") or ""),
        "release_id": str(release.get("release_id", "")),
        "release_hash": str(release.get("release_hash", "")),
        "qualification": str(release.get("qualification", "")),
    }
    if normalized["qualification"] not in {"PAPER_READY", "LIVE_READY"}:
        raise ChannelBindingError("策略资格不允许进入模拟交易")
    if not re.fullmatch(r"S[0-9]{3}", normalized["strategy_id"]):
        raise ChannelBindingError("strategy_id格式无效")
    if not re.fullmatch(r"v[1-9][0-9]*", normalized["strategy_version"]):
        raise ChannelBindingError("strategy_version格式无效")
    if normalized["release_id"] != f'{normalized["strategy_id"]}-{normalized["strategy_version"]}':
        raise ChannelBindingError("release_id与策略版本不一致")
    if not re.fullmatch(r"[0-9a-f]{64}", normalized["release_hash"]):
        raise ChannelBindingError("release_hash格式无效")
    return normalized


def bind_channel_strategy(store, release, actor: str, reason: str, active_orders):
    values = _validate_release(release)
    if not actor.strip() or not reason.strip():
        raise ChannelBindingError("actor和reason不能为空")
    current = load_channel_binding(store)
    if current and (current.release_id, current.release_hash) == (
        values["release_id"], values["release_hash"],
    ):
        return current
    if any(str(order.get("status", "")).upper() not in TERMINAL_STATUSES for order in active_orders):
        raise ChannelBindingError("存在活动渠道订单，拒绝切换策略绑定")
    binding = ChannelStrategyBinding(
        **values,
        bound_at=datetime.now(timezone.utc).isoformat(),
        bound_by=actor.strip(),
        reason=reason.strip(),
    )
    store.set_setting(BINDING_KEY, json.dumps(binding.to_dict(), ensure_ascii=False))
    AuditRecorder(store).record(
        "CHANNEL_STRATEGY_BOUND", source="channel_binding", actor_type="OPERATOR",
        actor_id=binding.bound_by, strategy_id=binding.strategy_id,
        strategy_version=binding.strategy_version, release_hash=binding.release_hash,
        channel="futu", correlation_id=f"binding:{binding.release_id}:{binding.bound_at}",
        details={
            "old_binding": None if current is None else current.to_dict(),
            "new_binding": binding.to_dict(), "reason": binding.reason,
        },
    )
    return binding


def migrate_channel_binding(store) -> ChannelStrategyBinding | None:
    current = load_channel_binding(store)
    if current is not None:
        return current
    snapshot = store.latest_snapshot() or {}
    strategy = (snapshot.get("last_decision") or {}).get("strategy") or {}
    if not strategy:
        return None
    release = dict(strategy)
    release.setdefault("release_id", f'{release.get("strategy_id", "")}-{release.get("version", "")}')
    release.setdefault("qualification", "PAPER_READY")
    return bind_channel_strategy(store, release, "migration", "从最近一次Futu渠道决策迁移", [])
