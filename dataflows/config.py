from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


DEFAULT_TOKEN_FILE = Path(__file__).with_name(".env")


def get_tushare_token(env_file: str | Path | None = None) -> str:
    """Return the Tushare token, preferring the process environment."""

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    token_file = Path(env_file) if env_file is not None else DEFAULT_TOKEN_FILE
    if not token and token_file.is_file():
        token = str(dotenv_values(token_file).get("TUSHARE_TOKEN") or "").strip()
    if not token:
        raise ValueError(
            "TUSHARE_TOKEN is not configured. Set the environment variable or fill "
            f"{DEFAULT_TOKEN_FILE}."
        )
    return token
