"""Preregistered downside-risk position optimization for 588080.SH."""

from __future__ import annotations

from collections.abc import Mapping
from itertools import product

import pandas as pd

from .downside_risk import DownsideRiskSpec
from .position_sizing_runner import EXPECTED_CHAMPION


LOOKBACKS = (10, 20, 40)
STRESS_QUANTILES = (0.7, 0.8, 0.9)
PRESSURE_POSITIONS = (0.25, 0.5, 0.75)
ANNUAL_WINDOWS = ("2021", "2022", "2023", "2024", "2025")
DIAGNOSTIC_WINDOWS = ("2026Q1", "2026H1", "2026M1-M8")
EXPECTED_ELIGIBILITY = (
    "full_period_challenger_return_greater_than_or_equal_to_champion",
    "full_period_challenger_max_drawdown_strictly_greater_than_champion",
)
EXPECTED_RANKING = (
    "full_period_max_drawdown_improvement_desc",
    "worst_annual_return_delta_desc",
    "full_period_trade_count_asc",
    "candidate_id_asc",
)


def build_downside_risk_specs(
    protocol: Mapping[str, object],
) -> tuple[DownsideRiskSpec, ...]:
    """Build the exact preregistered 27-candidate grid."""
    return tuple(
        DownsideRiskSpec(int(lookback), float(quantile), float(position))
        for lookback, quantile, position in product(
            protocol["downside_lookbacks"],  # type: ignore[arg-type]
            protocol["stress_quantiles"],  # type: ignore[arg-type]
            protocol["pressure_positions"],  # type: ignore[arg-type]
        )
    )


def validate_downside_risk_protocol(protocol: Mapping[str, object]) -> None:
    """Reject any drift from the committed 0830_EX01 protocol."""
    expected_scalars = {
        "schema_version": 1,
        "experiment_id": "0830_EX01",
        "handler": "downside_risk_position_optimization",
        "experiment_type": "downside_risk_position_optimization",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "factor_policy": "exact_active_baseline_signals_weights_normalization_and_state_machine",
        "position_policy": "champion_binary_gate_times_downside_risk_multiplier",
        "warmup_start": "2020-01-01",
        "selection_start": "2021-01-01",
        "selection_end": "2025-12-31",
        "selection_full_window": "2021-2025FULL",
        "downside_formula": "sqrt(252 * rolling_mean(min(log_return, 0)^2, lookback))",
        "threshold_policy": "strictly_greater_than_quantile_of_prior_252_valid_downside_volatility_values",
        "stress_history": 252,
        "candidate_count": 27,
        "no_eligible_policy": "fail_without_freeze_or_2026_access",
        "holdout_main_window": "2026FULL",
        "holdout_cutoff": "2026-08-28",
        "fee_rate": 0.0005,
        "init_cash": 1_000_000.0,
        "holdout_access_before_freeze": False,
        "pass_rule": "2026FULL_challenger_return_greater_than_or_equal_to_champion_and_max_drawdown_strictly_greater_than_champion",
        "observed_data_disclosure": (
            "2026 data through 2026-08-28 was already observed before this "
            "experiment; it is used only after 2021-2025 selection and freeze "
            "as the final historical backtest"
        ),
    }
    for key, expected in expected_scalars.items():
        if protocol.get(key) != expected:
            raise ValueError(f"downside-risk protocol differs for {key}")
    if protocol.get("champion") != EXPECTED_CHAMPION:
        raise ValueError("downside-risk champion identity differs")
    expected_sequences = {
        "annual_windows": ANNUAL_WINDOWS,
        "downside_lookbacks": LOOKBACKS,
        "stress_quantiles": STRESS_QUANTILES,
        "pressure_positions": PRESSURE_POSITIONS,
        "selection_eligibility": EXPECTED_ELIGIBILITY,
        "candidate_ranking": EXPECTED_RANKING,
        "holdout_diagnostic_windows": DIAGNOSTIC_WINDOWS,
    }
    for key, expected in expected_sequences.items():
        if tuple(protocol.get(key, ())) != expected:
            raise ValueError(f"downside-risk protocol differs for {key}")
    specs = build_downside_risk_specs(protocol)
    if len(specs) != 27 or len({spec.candidate_id for spec in specs}) != 27:
        raise ValueError("downside-risk protocol must build 27 unique candidates")


def rank_eligible_candidates(rows: pd.DataFrame) -> pd.DataFrame:
    """Filter hard constraints before applying the preregistered stable ranking."""
    eligible = rows.loc[
        rows["full_challenger_return"].ge(rows["full_champion_return"])
        & rows["full_challenger_max_drawdown"].gt(
            rows["full_champion_max_drawdown"]
        )
    ].copy()
    return eligible.sort_values(
        [
            "max_drawdown_improvement",
            "worst_annual_return_delta",
            "full_trade_count",
            "candidate_id",
        ],
        ascending=[False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def downside_risk_2026_pass(
    champion: Mapping[str, object],
    challenger: Mapping[str, object],
) -> bool:
    """Apply the preregistered 2026FULL return constraint and drawdown objective."""
    return (
        float(challenger["strategy_return"]) >= float(champion["strategy_return"])
        and float(challenger["max_drawdown"]) > float(champion["max_drawdown"])
    )
