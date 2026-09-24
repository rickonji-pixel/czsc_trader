"""Credential loading for optional market-data providers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True, slots=True)
class LongbridgeCredentials:
    app_key: str
    app_secret: str
    access_token: str


def get_longbridge_credentials(env_file: str | Path | None = None) -> LongbridgeCredentials:
    """Load legacy OpenAPI credentials without exposing them in errors or logs."""

    token_file = Path(env_file) if env_file is not None else None
    file_values = dotenv_values(token_file) if token_file is not None and token_file.is_file() else {}
    names = ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN")
    values = {name: str(os.getenv(name) or file_values.get(name) or "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"Longbridge credentials are incomplete: {', '.join(missing)}")
    return LongbridgeCredentials(*(values[name] for name in names))


def get_tushare_token(env_file: str | Path | None = None) -> str:
    """Return the Tushare token, preferring the process environment."""

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    token_file = Path(env_file) if env_file is not None else None
    if not token and token_file is not None and token_file.is_file():
        token = str(dotenv_values(token_file).get("TUSHARE_TOKEN") or "").strip()
    if not token:
        raise ValueError(
            "TUSHARE_TOKEN is not configured. Set the environment variable or fill "
            f"{token_file or 'an explicit env file'}."
        )
    return token
