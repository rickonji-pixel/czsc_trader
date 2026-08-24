"""Causal daily factor snapshots generated exclusively by CZSC."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import re

import czsc
import pandas as pd

from .data import MarketData


INTRADAY_CONFIG = [
    {"name": "cxt_bi_status_V230101", "freq": "30分钟"},
    {"name": "cxt_third_buy_V230228", "freq": "30分钟", "di": 1},
]
DAILY_CONFIG = [
    {"name": "cxt_bi_status_V230101", "freq": "日线"},
    {"name": "cxt_five_bi_V230619", "freq": "日线", "di": 1},
    {"name": "cxt_seven_bi_V230620", "freq": "日线", "di": 1},
    *[
        {"name": "tas_ma_base_V221101", "freq": "日线", "di": 1, "timeperiod": period, "ma_type": "SMA"}
        for period in (5, 10, 20)
    ],
    {
        "name": "tas_macd_base_V221028",
        "freq": "日线",
        "di": 1,
        "fastperiod": 12,
        "slowperiod": 26,
        "signalperiod": 9,
    },
    {"name": "vol_window_V230731", "freq": "日线", "di": 1, "w": 5, "m": 30, "n": 10},
    {"name": "pressure_support_V240406", "freq": "日线", "di": 1, "w": 20},
]
WEEKLY_CONFIG = [{"name": "cxt_bi_status_V230101", "freq": "周线"}]


@dataclass(frozen=True)
class FactorResult:
    """Daily grouped factors plus diagnostics about signal categories."""

    frame: pd.DataFrame
    unknown_values: dict[str, int]


def _to_raw_bars(frame: pd.DataFrame, freq: str) -> list:
    columns = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
    return czsc.format_standard_kline(frame[columns], freq=freq)


def _config_label(freq_tag: str, config: dict) -> str:
    parameters = [
        f"{key}_{config[key]}"
        for key in sorted(config)
        if key not in {"name", "freq"}
    ]
    suffix = "__" + "__".join(parameters) if parameters else ""
    return f"raw__{freq_tag}__{config['name']}{suffix}"


def _run_signals(frame: pd.DataFrame, freq: str, configs: list[dict], init_n: int, freq_tag: str) -> pd.DataFrame:
    bars = _to_raw_bars(frame, freq)
    pieces: list[pd.Series] = []
    for config in configs:
        # CZSC does not guarantee signal-column order when several configs are
        # evaluated together.  Run each registered signal independently so a
        # category can never be attached to the wrong factor label.
        output = czsc.generate_czsc_signals(
            bars,
            [config],
            sdt=str(frame["dt"].min().date()),
            init_n=init_n,
            df=True,
        )
        output_index = pd.DatetimeIndex(pd.to_datetime(output["dt"]), name="dt")
        if output_index.tz is not None:
            # PyO3 labels the original naive wall-clock values as UTC.  The
            # clock values already match the CSV, so drop rather than convert.
            output_index = output_index.tz_localize(None)
        signal_columns = [column for column in output.columns if len(column.split("_")) == 3]
        if len(signal_columns) != 1:
            raise ValueError(f"CZSC {freq} returned unexpected signal columns for {config}: {signal_columns}")
        pieces.append(
            pd.Series(
                output[signal_columns[0]].astype("string").to_numpy(),
                index=output_index,
                name=_config_label(freq_tag, config),
            )
        )
    return pd.concat(pieces, axis=1)


def signal_primary(value: object) -> str | None:
    """Return the primary CZSC category without assigning trading direction."""
    if value is None or pd.isna(value):
        return None
    return str(value).split("_", 1)[0]


def _signal_score(value: object, unknown: Counter[str]) -> float:
    primary = signal_primary(value)
    if primary is None:
        return 0.0
    if any(token in primary for token in ("向上", "多头", "三买", "底背驰", "支撑位", "强势")):
        return 1.0
    if any(token in primary for token in ("向下", "空头", "顶背驰", "压力位", "弱势")):
        return -1.0
    volume_rank = re.search(r"高量N(\d+)", primary)
    if volume_rank:
        rank = int(volume_rank.group(1))
        return 1.0 if rank >= 7 else -1.0 if rank <= 3 else 0.0
    if any(token in primary for token in ("其他", "任意", "中性", "零轴附近")):
        return 0.0
    unknown[primary] += 1
    return 0.0


def map_signal_frame(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Apply the champion's frozen semantic mapping to raw CZSC signals."""
    unknown: Counter[str] = Counter()
    mapped = raw.map(lambda value: _signal_score(value, unknown)).astype(float)
    return mapped, dict(sorted(unknown.items()))


def signal_groups(columns: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Return the frozen aggregate-factor membership for raw signal columns."""
    names = tuple(str(column) for column in columns)
    return {
        "structure": tuple(column for column in names if "cxt_" in column),
        "trend": tuple(
            column
            for column in names
            if "tas_ma_" in column or "tas_macd_" in column
        ),
        "volume_position": tuple(
            column
            for column in names
            if "vol_window_" in column or "pressure_support_" in column
        ),
    }


def aggregate_signal_groups(
    mapped: pd.DataFrame,
    groups: Mapping[str, Sequence[str]],
) -> pd.DataFrame:
    """Aggregate mapped signals exactly as the frozen champion factor layer."""
    aggregated = pd.DataFrame(index=mapped.index)
    for name in ("structure", "trend", "volume_position"):
        aggregated[name] = mapped.loc[:, list(groups[name])].mean(axis=1).fillna(0.0).clip(-1.0, 1.0)
    return aggregated


def _backward_align(source: pd.DataFrame, target_index: pd.DatetimeIndex) -> pd.DataFrame:
    left = pd.DataFrame({"dt": target_index})
    right = source.reset_index().sort_values("dt")
    return pd.merge_asof(left.sort_values("dt"), right, on="dt", direction="backward").set_index("dt")


def generate_factor_frame(data: MarketData) -> FactorResult:
    """Generate one factor snapshot per completed trade date without backfilling warm-up."""
    target_index = pd.DatetimeIndex(data.daily["dt"], name="dt")

    intraday = _run_signals(data.intraday, "30分钟", INTRADAY_CONFIG, init_n=500, freq_tag="30m")
    intraday = intraday.loc[intraday.index.strftime("%H:%M") == "15:00"]
    intraday.index = intraday.index.normalize()
    daily = _run_signals(data.daily, "日线", DAILY_CONFIG, init_n=30, freq_tag="daily")
    daily.index = daily.index.normalize()
    weekly = _run_signals(data.weekly, "周线", WEEKLY_CONFIG, init_n=10, freq_tag="weekly")
    weekly.index = weekly.index.normalize()

    raw = pd.concat(
        [
            intraday.reindex(target_index),
            daily.reindex(target_index),
            _backward_align(weekly, target_index),
        ],
        axis=1,
    )
    raw.index = target_index

    scored, unknown = map_signal_frame(raw)
    groups = aggregate_signal_groups(scored, signal_groups(scored.columns))
    groups["factor_coverage"] = raw.notna().mean(axis=1)

    frame = pd.concat([groups, raw], axis=1)
    frame.index = target_index
    return FactorResult(frame=frame, unknown_values=unknown)
