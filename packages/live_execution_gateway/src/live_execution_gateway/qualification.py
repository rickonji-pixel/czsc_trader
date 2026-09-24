"""Fail-closed live qualification lookup from immutable strategy records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def strategy_identity(
    repo_root: str | Path, strategy_id: str, strategy_version: str,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() / "strategies" / strategy_id
    version_path = root / "versions" / f"{strategy_version}.json"
    lifecycle_path = root / "lifecycle.jsonl"
    version = json.loads(version_path.read_text(encoding="utf-8"))
    if version.get("strategy_id") != strategy_id or version.get("version") != strategy_version:
        raise ValueError("strategy version identity differs from requested live deployment")
    matching = []
    for line in lifecycle_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if (
            event.get("strategy_id") == strategy_id
            and event.get("version") == strategy_version
            and event.get("release_hash") == version.get("release_hash")
            and event.get("to_state") in {"PAPER_READY", "LIVE_READY", "RETIRED"}
        ):
            matching.append(event)
    if not matching:
        raise ValueError("strategy has no matching deployment lifecycle state")
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "release_id": version["release_id"],
        "release_hash": version["release_hash"],
        "selection_data_cutoff": version["selection_data_cutoff"],
        "qualification": matching[-1]["to_state"],
    }


def require_live_ready(identity: dict[str, Any]) -> None:
    if identity.get("qualification") != "LIVE_READY":
        raise PermissionError(
            f"strategy is {identity.get('qualification')}, not LIVE_READY; real orders are forbidden"
        )
