"""Read-only frozen strategy metadata used by SRT chart implementations."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


_STRATEGY_ID = re.compile(r"S\d+")
_VERSION = re.compile(r"v\d+")


class AccountChartStrategyMetadata:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def configuration(
        self, *, strategy_id: str, strategy_version: str, release_hash: str,
    ) -> dict[str, Any]:
        if _STRATEGY_ID.fullmatch(strategy_id) is None or _VERSION.fullmatch(
            strategy_version
        ) is None:
            raise ValueError("account chart strategy identity is invalid")
        path = (
            self.repo_root
            / "strategies"
            / strategy_id
            / "versions"
            / f"{strategy_version}.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("account chart strategy version is unavailable") from exc
        if not isinstance(value, dict) or value.get("release_hash") != release_hash:
            raise ValueError("account chart strategy version identity differs")
        payload = value.get("strategy_payload")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("account chart strategy configuration is unavailable")
        return dict(payload)
