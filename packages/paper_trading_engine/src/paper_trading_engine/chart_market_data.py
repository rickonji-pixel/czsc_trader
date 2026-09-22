"""Strategy-independent market data for PTE observation charts."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from dataflows import DataRequest, Dataflows, Dataset, canonical_frame_sha256


class AccountChartMarketData:
    """Read adjusted daily bars without creating or inspecting a strategy instance."""

    def __init__(
        self,
        *,
        dataflows: Dataflows | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.dataflows = dataflows or Dataflows()
        self.today = today or date.today

    def history(
        self,
        *,
        symbol: str,
        asset: str,
        selection_data_cutoff: str,
        context_sessions: int,
    ) -> tuple[str, object]:
        normalized_asset = asset.lower()
        if normalized_asset not in {"stock", "etf"}:
            raise ValueError("account chart asset must be stock or etf")
        cutoff = date.fromisoformat(selection_data_cutoff)
        end = max(cutoff, self.today())
        start = cutoff - timedelta(days=max(180, context_sessions * 3))
        dataset = Dataset.ETF_OHLCV if normalized_asset == "etf" else Dataset.STOCK_OHLCV
        result = self.dataflows.fetch(
            DataRequest(
                dataset,
                symbol.upper(),
                start.isoformat(),
                end.isoformat(),
                end.isoformat(),
                "daily",
            )
        )
        if not result.ready:
            message = result.error.message if result.error is not None else result.status
            raise ValueError(f"account chart market data is unavailable: {message}")
        frame = result.dataframe.copy()
        if frame.empty:
            raise ValueError("account chart market data is empty")
        return canonical_frame_sha256(frame), frame
