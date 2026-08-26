"""Shared Tushare client construction."""

from __future__ import annotations

from pathlib import Path

import tushare as ts

from .config import get_tushare_token


def get_tushare_module(env_file: str | Path | None = None):
    token = get_tushare_token(env_file)
    ts.set_token(token)
    return ts


def get_tushare_pro(env_file: str | Path | None = None):
    module = get_tushare_module(env_file)
    return module.pro_api(get_tushare_token(env_file))
