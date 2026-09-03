from __future__ import annotations

from .models import MetricComparison, MetricObservation, MetricStatus
from .standards import MarginSet

_EPSILON = 1e-12


def _comparison(metric: str, candidate: float | None, incumbent: float | None, threshold: float | None, window: str, comparable: bool = True) -> MetricComparison:
    if not comparable or candidate is None or incumbent is None or threshold is None:
        return MetricComparison(metric, window, candidate, incumbent, None, False, False, f"INSUFFICIENT_{metric.upper()}_{window.upper()}")
    scale = max(abs(incumbent - threshold), abs(incumbent) * 0.01, _EPSILON)
    score = (candidate - incumbent) / scale
    passed = candidate + _EPSILON >= threshold
    code = "PASS" if passed else f"NONINFERIORITY_{metric.upper()}_{window.upper()}"
    return MetricComparison(metric, window, candidate, incumbent, score, True, passed, code)


def compare_observation(candidate: MetricObservation, incumbent: MetricObservation, margins: MarginSet) -> tuple[MetricComparison, ...]:
    if candidate.window_id != incumbent.window_id:
        raise ValueError("observations must use the same window")
    window = candidate.window_id
    cagr_threshold = incumbent.net_cagr * margins.net_cagr_retention if incumbent.net_cagr > 0 else incumbent.net_cagr
    cagr = _comparison("net_cagr", candidate.net_cagr, incumbent.net_cagr, cagr_threshold, window)

    incumbent_magnitude = abs(incumbent.max_drawdown)
    allowed_magnitude = min(incumbent_magnitude + margins.max_drawdown_absolute, incumbent_magnitude * (1 + margins.max_drawdown_relative))
    drawdown = _comparison("max_drawdown", candidate.max_drawdown, incumbent.max_drawdown, -allowed_magnitude, window)

    calmar_comparable = candidate.calmar_status is MetricStatus.VALID and incumbent.calmar_status is MetricStatus.VALID
    if incumbent.calmar is None:
        calmar_threshold = None
    elif incumbent.calmar > 0:
        calmar_threshold = incumbent.calmar * margins.calmar_retention
    elif margins.negative_calmar_requires_positive:
        calmar_threshold = 0.0
    else:
        calmar_threshold = incumbent.calmar
    calmar = _comparison("calmar", candidate.calmar, incumbent.calmar, calmar_threshold, window, calmar_comparable)

    pf_comparable = (
        candidate.profit_factor_status is MetricStatus.VALID
        and incumbent.profit_factor_status is MetricStatus.VALID
        and candidate.closed_trades >= margins.minimum_closed_trades
    )
    pf_threshold = None if incumbent.profit_factor is None else max(incumbent.profit_factor * margins.profit_factor_retention, margins.profit_factor_floor)
    profit_factor = _comparison("profit_factor", candidate.profit_factor, incumbent.profit_factor, pf_threshold, window, pf_comparable)
    return cagr, drawdown, calmar, profit_factor
