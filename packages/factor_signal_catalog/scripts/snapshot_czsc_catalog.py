from __future__ import annotations

import argparse
import json
from pathlib import Path

import czsc


CURATED = {
    "cxt_bi_status_V230101": ("MARKET_STRUCTURE", "CZSC笔方向状态"),
    "cxt_third_buy_V230228": ("MARKET_STRUCTURE", "CZSC三买结构"),
    "cxt_five_bi_V230619": ("MARKET_STRUCTURE", "CZSC五笔形态"),
    "cxt_seven_bi_V230620": ("MARKET_STRUCTURE", "CZSC七笔形态"),
    "cxt_bi_base_V230228": ("MARKET_STRUCTURE", "CZSC当前笔方向"),
    "tas_ma_base_V221101": ("TREND_MOMENTUM", "价格与移动平均线关系"),
    "tas_macd_base_V221028": ("TREND_MOMENTUM", "MACD趋势动量状态"),
    "vol_window_V230731": ("VOLUME_LIQUIDITY", "滚动成交量窗口位置"),
    "pressure_support_V240406": ("POSITION_VALUATION", "支撑压力相对位置"),
    "emv_up_dw_line_V230605": ("VOLUME_LIQUIDITY", "EMV量价运动效率方向"),
}


def _family(name: str, namespace: str) -> str:
    if name in CURATED:
        return CURATED[name][0]
    lowered = f"{namespace}_{name}".lower()
    if any(token in lowered for token in ("cxt", "bi_", "fx_", "support", "pressure")):
        return "MARKET_STRUCTURE"
    if any(token in lowered for token in ("vol", "amount", "emv", "obv", "mfi", "vwap")):
        return "VOLUME_LIQUIDITY"
    if any(token in lowered for token in ("ma_", "macd", "cci", "kdj", "rsi", "trend", "momentum")):
        return "TREND_MOMENTUM"
    return "OTHER_TECHNICAL"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = []
    for entry in sorted(czsc._native.list_all_signals(), key=lambda item: item["name"]):
        name = str(entry["name"])
        namespace = str(entry.get("namespace", "unknown"))
        curated = CURATED.get(name)
        rows.append({
            "signal_id": f"SIG-CZSC-{name}",
            "name": name,
            "description": curated[1] if curated else f"CZSC注册信号；命名空间{namespace}，金融语义待人工复核",
            "information_family": _family(name, namespace),
            "tags": ["czsc", str(entry.get("category", "unknown")), namespace],
            "factor_ids": [],
            "embedded_factor": True,
            "provider": "czsc",
            "implementation": f"czsc._native::{name}",
            "rule": "由CZSC注册信号函数内部计算因子并输出离散状态",
            "states": [],
            "parameters": {"template": str(entry.get("param_template", ""))},
            "availability": "对应周期K线完成后",
            "causality": "调用方必须只提供当时已完成K线",
            "status": "READY" if curated else "DISCOVERED",
            "version": 1,
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {"schema_version": 1, "source": {"package": "czsc", "version": czsc.__version__}, "items": rows},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
