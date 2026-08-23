from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd


def format_metadata_lines(metadata: dict[str, Any] | None = None) -> str:
    metadata = metadata or {}
    lines = [f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    for key, value in metadata.items():
        if value is not None:
            lines.append(f"# {key}: {value}")
    return "\n".join(lines)


def format_dataframe_report(
    title: str,
    dataframe: pd.DataFrame,
    metadata: dict[str, Any] | None = None,
    *,
    index: bool = False,
    max_rows: int = 200,
) -> str:
    if dataframe is None or dataframe.empty:
        return f"No data available for {title}."

    trimmed = dataframe.head(max_rows)
    header = f"# {title}\n{format_metadata_lines(metadata)}\n\n"
    return header + trimmed.to_csv(index=index)


def format_text_report(title: str, lines: list[str], metadata: dict[str, Any] | None = None) -> str:
    header = f"# {title}\n{format_metadata_lines(metadata)}\n\n"
    body = "\n".join(line for line in lines if line)
    if not body:
        body = "No data available."
    return header + body


def format_json_report(title: str, payload: Any, metadata: dict[str, Any] | None = None) -> str:
    header = f"# {title}\n{format_metadata_lines(metadata)}\n\n"
    return header + json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def unsupported_response(
    vendor: str, method: str, market: str | None = None, reason: str | None = None
) -> str:
    details: list[str] = [f"Vendor `{vendor}` does not currently support `{method}`"]
    if market:
        details.append(f"for market `{market}`")
    message = " ".join(details) + "."
    if reason:
        message += f" {reason}"
    return message


def standardize_ohlcv_dataframe(
    dataframe: pd.DataFrame,
    mapping: dict[str, str],
    *,
    date_column: str = "Date",
) -> pd.DataFrame:
    renamed = dataframe.rename(columns=mapping).copy()
    required = [date_column, "Open", "High", "Low", "Close", "Volume"]
    for column in required:
        if column not in renamed.columns:
            if column == "Volume":
                renamed[column] = 0
            else:
                raise ValueError(f"Missing required OHLCV column: {column}")

    renamed[date_column] = pd.to_datetime(renamed[date_column], errors="coerce")
    renamed = renamed.dropna(subset=[date_column]).sort_values(date_column)

    numeric_columns = [
        column
        for column in ["Open", "High", "Low", "Close", "Volume", "Adj Close", "Amount"]
        if column in renamed.columns
    ]
    for column in numeric_columns:
        renamed[column] = pd.to_numeric(renamed[column], errors="coerce")

    renamed["Date"] = renamed[date_column].dt.strftime("%Y-%m-%d")
    base_columns = [
        column
        for column in ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Amount"]
        if column in renamed.columns
    ]
    selected = renamed[base_columns]
    if isinstance(selected, pd.Series):
        return selected.to_frame().reset_index(drop=True)
    return selected.reset_index(drop=True)
