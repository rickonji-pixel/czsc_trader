from __future__ import annotations

import tushare as ts

from .config import get_tushare_token


def get_tushare_module():
    token = get_tushare_token()
    ts.set_token(token)
    return ts


def get_tushare_pro():
    module = get_tushare_module()
    return module.pro_api(get_tushare_token())
