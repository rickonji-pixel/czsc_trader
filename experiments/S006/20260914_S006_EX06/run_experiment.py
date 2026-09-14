from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd
import statsmodels.api as sm

from factor_signal_catalog import CatalogRegistry

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX06"
PLACEHOLDER_STATES = {"warmup"}


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    frame = pd.concat([left.rename("left"), right.rename("right")], axis=1).dropna()
    if len(frame) < 3 or frame["left"].nunique() < 2 or frame["right"].nunique() < 2:
        return None
    return float(frame["left"].corr(frame["right"], method="spearman"))


def _hac(score: pd.Series, outcome: pd.Series, max_lags: int) -> tuple[float | None, float | None]:
    frame = pd.concat([score.rename("score"), outcome.rename("outcome")], axis=1).dropna()
    if len(frame) < 20 or frame["score"].nunique() < 2:
        return None, None
    standard_deviation = float(frame["score"].std(ddof=0))
    if standard_deviation <= 0.0:
        return None, None
    normalized = (frame["score"] - frame["score"].mean()) / standard_deviation
    fitted = sm.OLS(
        frame["outcome"].to_numpy(dtype=float),
        sm.add_constant(normalized.to_numpy(dtype=float)),
    ).fit(cov_type="HAC", cov_kwds={"maxlags": int(max_lags)})
    coefficient = float(fitted.params[1])
    two_sided = float(fitted.pvalues[1])
    return coefficient, two_sided / 2.0 if coefficient >= 0 else 1.0 - two_sided / 2.0


def _bh(values: pd.Series) -> pd.Series:
    result = pd.Series(np.nan, index=values.index, dtype=float)
    valid = values.dropna().sort_values()
    if valid.empty:
        return result
    count = len(valid)
    adjusted = valid.to_numpy(dtype=float) * count / np.arange(1, count + 1, dtype=float)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result.loc[valid.index] = np.minimum(adjusted, 1.0)
    return result


def _residualize(outcome: pd.Series, prices: pd.DataFrame, protocol: dict[str, object]) -> pd.Series:
    controls = protocol["controls"]
    close = prices["close"].astype(float)
    past = close.pct_change(int(controls["past_return_sessions"]), fill_method=None).reindex(outcome.index)
    volatility = close.pct_change(fill_method=None).rolling(int(controls["volatility_sessions"])).std().reindex(outcome.index)
    years = pd.get_dummies(outcome.index.year, prefix="year", drop_first=True, dtype=float)
    years.index = outcome.index
    frame = pd.concat(
        [outcome.rename("outcome"), past.rename("past_return"), volatility.rename("volatility"), years],
        axis=1,
    ).dropna()
    fitted = sm.OLS(frame["outcome"].astype(float), sm.add_constant(frame.drop(columns="outcome").astype(float))).fit()
    residual = pd.Series(np.nan, index=outcome.index, dtype=float)
    residual.loc[frame.index] = fitted.resid
    return residual


def _next_calendar_session(calendar: pd.DatetimeIndex, current: pd.Timestamp) -> pd.Timestamp | None:
    position = int(calendar.searchsorted(pd.Timestamp(current).normalize(), side="right"))
    return calendar[position] if position < len(calendar) else None


def _first_tradable_date(
    calendar: pd.DatetimeIndex,
    usable_date: object,
    usable_clock: object,
) -> pd.Timestamp | None:
    normalized = pd.Timestamp(usable_date).normalize()
    position = int(calendar.searchsorted(normalized, side="left"))
    if position >= len(calendar):
        return None
    candidate = calendar[position]
    clock = str(usable_clock).strip()
    if len(clock) == 5:
        clock += ":00"
    after_open = clock > "09:30:00"
    if candidate == normalized and after_open:
        position += 1
        if position >= len(calendar):
            return None
        candidate = calendar[position]
    return candidate


def _shift_after_close(series: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    mapped: dict[pd.Timestamp, object] = {}
    for source_date, value in series.dropna().items():
        target = _next_calendar_session(calendar, pd.Timestamp(source_date))
        if target is not None:
            mapped[target] = value
    return pd.Series(mapped, dtype="object").reindex(calendar)


def _map_first_usable(frame: pd.DataFrame, calendar: pd.DatetimeIndex, value_column: str) -> pd.Series:
    mapped: dict[pd.Timestamp, object] = {}
    for row in frame.itertuples(index=False):
        target = _first_tradable_date(calendar, row.first_usable_date, row.first_usable_clock)
        if target is not None:
            mapped[target] = getattr(row, value_column)
    return pd.Series(mapped).reindex(calendar)


def _empirical_score(train: pd.Series, apply: pd.Series, lower: float, upper: float) -> tuple[pd.Series, pd.Series]:
    training = train.dropna().astype(float)
    if training.empty:
        return pd.Series(np.nan, index=train.index), pd.Series(np.nan, index=apply.index)
    low = float(training.quantile(lower))
    high = float(training.quantile(upper))
    clipped = training.clip(low, high).sort_values().to_numpy(dtype=float)
    if len(np.unique(clipped)) < 2:
        return pd.Series(np.nan, index=train.index), pd.Series(np.nan, index=apply.index)

    def score(values: pd.Series) -> pd.Series:
        numeric = values.astype(float).clip(low, high)
        ranked = np.searchsorted(clipped, numeric.to_numpy(dtype=float), side="right") / len(clipped)
        output = pd.Series(ranked - 0.5, index=values.index, dtype=float)
        output.loc[values.isna()] = np.nan
        return output

    return score(train), score(apply)


def _causal_percentile(values: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float(
            (np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current))
            / len(valid)
        )

    return values.astype(float).rolling(window, min_periods=minimum).apply(rank_last, raw=True)


def _annual_ics(score: pd.Series, outcome: pd.Series, minimum: int) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for year, year_outcome in outcome.groupby(outcome.index.year):
        year_score = score.reindex(year_outcome.index)
        frame = pd.concat([year_score, year_outcome], axis=1).dropna()
        values[str(int(year))] = _spearman(frame.iloc[:, 0], frame.iloc[:, 1]) if len(frame) >= minimum else None
    return values


def _audit_path(
    *,
    component_id: str,
    catalog_id: str,
    kind: str,
    family: str,
    horizon: int,
    discovery_score: pd.Series,
    confirmation_score: pd.Series,
    discovery_outcome: pd.Series,
    confirmation_outcome: pd.Series,
    confirmation_residual: pd.Series,
    minimum_observations: int,
    annual_minimum: int,
    max_lags: int,
    metadata: dict[str, object],
) -> dict[str, object]:
    discovery_frame = pd.concat([discovery_score.rename("score"), discovery_outcome.rename("outcome")], axis=1).dropna()
    confirmation_frame = pd.concat([confirmation_score.rename("score"), confirmation_outcome.rename("outcome")], axis=1).dropna()
    identifiable = len(confirmation_frame) >= minimum_observations and confirmation_frame["score"].nunique() >= 2
    discovery_ic = _spearman(discovery_frame["score"], discovery_frame["outcome"])
    confirmation_ic = _spearman(confirmation_frame["score"], confirmation_frame["outcome"])
    residual_ic = _spearman(confirmation_score, confirmation_residual)
    coefficient, pvalue = _hac(confirmation_score, confirmation_outcome, max_lags) if identifiable else (None, None)
    annual = _annual_ics(confirmation_score, confirmation_outcome, annual_minimum)
    positive_years = sum(value is not None and value > 0 for value in annual.values())
    valid_scores = confirmation_frame["score"]
    if valid_scores.nunique() >= 5:
        low = float(valid_scores.quantile(0.2))
        high = float(valid_scores.quantile(0.8))
        top = confirmation_frame.loc[valid_scores.ge(high), "outcome"]
        bottom = confirmation_frame.loc[valid_scores.le(low), "outcome"]
    else:
        top = confirmation_frame.loc[valid_scores.gt(0), "outcome"]
        bottom = confirmation_frame.loc[valid_scores.lt(0), "outcome"]
    spread = float(top.mean() - bottom.mean()) if len(top) and len(bottom) else None
    return {
        "path_id": f"{component_id}:H{horizon}",
        "component_id": component_id,
        "catalog_id": catalog_id,
        "kind": kind,
        "information_family": family,
        "horizon_sessions": horizon,
        "discovery_observations": int(len(discovery_frame)),
        "confirmation_observations": int(len(confirmation_frame)),
        "discovery_ic": discovery_ic,
        "confirmation_ic": confirmation_ic,
        "confirmation_residual_ic": residual_ic,
        "hac_coefficient": coefficient,
        "hac_one_sided_pvalue": pvalue,
        "positive_confirmation_years": int(positive_years),
        "annual_ics": annual,
        "top_observations": int(len(top)),
        "bottom_observations": int(len(bottom)),
        "top_minus_bottom_return": spread,
        "identifiable": bool(identifiable),
        **metadata,
    }


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if not protocol.get("reads_new_returns"):
        raise ValueError("EX06 must explicitly declare return access")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX06 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    amendment = repo / "experiments/S006" / str(sources["execution_amendment_experiment_id"])
    validate_experiment_archive(amendment)
    frozen_files = {
        amendment / "experiment_manifest.json": sources["execution_amendment_manifest_sha256"],
        repo / str(sources["project_signal_states_path"]): sources["project_signal_states_sha256"],
        repo / str(sources["czsc_primary_states_path"]): sources["czsc_primary_states_sha256"],
        repo / str(sources["czsc_signal_catalog_path"]): sources["czsc_signal_catalog_sha256"],
        repo / str(sources["project_factor_ledger_path"]): sources["project_factor_ledger_sha256"],
        repo / str(sources["industry_factor_path"]): sources["industry_factor_sha256"],
        repo / str(sources["analyst_factor_path"]): sources["analyst_factor_sha256"],
    }
    for path, expected in frozen_files.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(
        context,
        str(protocol["dataset"]["name"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    if replay.fingerprint != str(protocol["dataset"]["fingerprint"]):
        raise ValueError("research dataset differs from frozen protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    calendar = prices.index
    opens = prices["open"].astype(float)

    dataset = protocol["dataset"]
    discovery_dates = (calendar >= pd.Timestamp(dataset["discovery_start"])) & (calendar <= pd.Timestamp(dataset["discovery_end"]))
    confirmation_dates = (calendar >= pd.Timestamp(dataset["confirmation_start"])) & (calendar <= pd.Timestamp(dataset["confirmation_end"]))
    horizons = [int(value) for value in protocol["universe"]["forward_open_intervals"]]
    outcomes = {horizon: opens.shift(-horizon).div(opens).sub(1.0) for horizon in horizons}
    residuals = {
        horizon: _residualize(outcomes[horizon].loc[confirmation_dates].dropna(), prices, protocol)
        for horizon in horizons
    }

    registry = CatalogRegistry(repo / "catalog")
    families = {row["id"]: row["information_family"] for row in registry.list_definitions(kind="signal")}
    primary = pd.read_csv(repo / str(sources["czsc_primary_states_path"]))
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(calendar)
    signal_catalog = pd.read_csv(repo / str(sources["czsc_signal_catalog_path"]))
    generated = signal_catalog.loc[signal_catalog["status"].eq("GENERATED")].copy()
    if len(generated) != int(protocol["universe"]["czsc_categorical_configurations"]):
        raise ValueError("CZSC configuration count differs from protocol")

    categorical: list[dict[str, object]] = []
    for row in generated.itertuples(index=False):
        raw = _shift_after_close(primary[str(row.signal_id)], calendar)
        categorical.append({
            "component_id": str(row.signal_id),
            "catalog_id": f"SIG-CZSC-{row.name}",
            "family": families[f"SIG-CZSC-{row.name}"],
            "frequency": str(row.frequency),
            "states": raw,
        })
    project_states = pd.read_csv(repo / str(sources["project_signal_states_path"]), parse_dates=["first_usable_date"])
    for signal_id, group in project_states.groupby("signal_id", sort=True):
        raw = _map_first_usable(group, calendar, "state")
        raw.loc[raw.astype(str).isin(PLACEHOLDER_STATES)] = np.nan
        categorical.append({
            "component_id": str(signal_id),
            "catalog_id": str(signal_id),
            "family": families[str(signal_id)],
            "frequency": "project",
            "states": raw,
        })
    if len(categorical) != int(protocol["universe"]["czsc_categorical_configurations"]) + int(protocol["universe"]["project_categorical_configurations"]):
        raise ValueError("categorical configuration count differs from protocol")

    factor_ledger = pd.read_csv(repo / str(sources["project_factor_ledger_path"]))
    factor_ledger = factor_ledger.loc[factor_ledger["value_numeric"].notna()].copy()
    continuous: dict[str, pd.Series] = {}
    for factor_id, group in factor_ledger.groupby("factor_id", sort=True):
        continuous[str(factor_id)] = _map_first_usable(group, calendar, "value_numeric").astype(float)
    if len(continuous) != 12:
        raise ValueError("numeric project factor count differs from materialization evidence")

    industry = pd.read_csv(repo / str(sources["industry_factor_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
    breadth = _causal_percentile(industry["positive_member_ratio"], 252, 126)
    flow = _causal_percentile(industry["net_flow_ratio"], 252, 126)
    industry_raw = pd.concat([breadth, flow], axis=1).mean(axis=1, skipna=False)
    industry_value = _causal_percentile(industry_raw, 252, 126)
    industry_dates = pd.Series(industry["trade_date"].to_numpy(), index=industry.index)
    industry_frame = pd.DataFrame({
        "first_usable_date": industry_dates.map(lambda value: _next_calendar_session(calendar, pd.Timestamp(value))),
        "first_usable_clock": "09:00:00",
        "value_numeric": industry_value,
    }).dropna(subset=["first_usable_date"])
    continuous["F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW"] = _map_first_usable(industry_frame, calendar, "value_numeric").astype(float)

    analyst = pd.read_csv(repo / str(sources["analyst_factor_path"]), parse_dates=["trade_date"]).sort_values("trade_date")
    analyst_frame = pd.DataFrame({
        "first_usable_date": analyst["trade_date"],
        "first_usable_clock": "09:00:00",
        "value_numeric": analyst["revision_score"],
    })
    continuous["F-PROJECT-SELL-SIDE-REVISION-BREADTH"] = _map_first_usable(analyst_frame, calendar, "value_numeric").astype(float)
    if len(continuous) != int(protocol["universe"]["continuous_factors"]):
        raise ValueError("continuous factor count differs from protocol")

    news = pd.read_csv(repo / str(sources["project_factor_ledger_path"]))
    news = news.loc[news["factor_id"].eq("F-PROJECT-STRUCTURED-NEWS-EVENT")].copy()
    news["event_sign"] = news["value_text"].astype(str).str.split("|").str[0].map({"POSITIVE": 1.0, "NEGATIVE": -1.0, "NEUTRAL": 0.0})
    event_rows: list[dict[str, object]] = []
    for row in news.dropna(subset=["event_sign"]).itertuples(index=False):
        target = _first_tradable_date(calendar, row.first_usable_date, row.first_usable_clock)
        if target is not None:
            event_rows.append({"date": target, "score": float(row.event_sign)})
    event_frame = pd.DataFrame(event_rows)
    event_score = event_frame.groupby("date")["score"].mean().replace(0.0, np.nan).reindex(calendar)

    model = protocol["model"]
    statistics = protocol["statistics"]
    result_rows: list[dict[str, object]] = []
    state_edge_rows: list[dict[str, object]] = []
    factor_model_rows: list[dict[str, object]] = []

    for horizon in horizons:
        outcome = outcomes[horizon]
        discovery_outcome = outcome.loc[discovery_dates].dropna()
        confirmation_outcome = outcome.loc[confirmation_dates].dropna()
        confirmation_residual = residuals[horizon]
        baseline = float(discovery_outcome.mean())
        prior = float(model["categorical_state_shrinkage_prior_count"])
        for component in categorical:
            states = component["states"]
            train_states = states.reindex(discovery_outcome.index)
            state_edges: dict[str, float] = {}
            for state in sorted(str(value) for value in train_states.dropna().unique()):
                active = train_states.astype(str).eq(state) & train_states.notna()
                count = int(active.sum())
                raw_edge = float(discovery_outcome.loc[active].mean() - baseline) if count else 0.0
                shrinkage = count / (count + prior) if count else 0.0
                state_edges[state] = raw_edge * shrinkage
                state_edge_rows.append({
                    "component_id": component["component_id"],
                    "catalog_id": component["catalog_id"],
                    "horizon_sessions": horizon,
                    "state": state,
                    "discovery_occurrences": count,
                    "raw_edge": raw_edge,
                    "shrinkage": shrinkage,
                    "frozen_score": state_edges[state],
                })
            discovery_score = train_states.astype(str).map(state_edges).where(train_states.notna())
            confirmation_states = states.reindex(confirmation_outcome.index)
            confirmation_score = confirmation_states.astype(str).map(state_edges).where(confirmation_states.notna())
            result_rows.append(_audit_path(
                component_id=str(component["component_id"]),
                catalog_id=str(component["catalog_id"]),
                kind="CATEGORICAL_SIGNAL",
                family=str(component["family"]),
                horizon=horizon,
                discovery_score=discovery_score,
                confirmation_score=confirmation_score,
                discovery_outcome=discovery_outcome,
                confirmation_outcome=confirmation_outcome,
                confirmation_residual=confirmation_residual,
                minimum_observations=int(model["minimum_confirmation_observations"]),
                annual_minimum=int(model["minimum_annual_observations"]),
                max_lags=int(statistics["hac_max_lags"]),
                metadata={"frequency": component["frequency"], "orientation": "STATE_EDGE_LEARNED_IN_DISCOVERY"},
            ))

        for factor_id, raw in continuous.items():
            discovery_raw = raw.reindex(discovery_outcome.index)
            confirmation_raw = raw.reindex(confirmation_outcome.index)
            discovery_score, confirmation_score = _empirical_score(
                discovery_raw,
                confirmation_raw,
                float(model["continuous_winsor_lower"]),
                float(model["continuous_winsor_upper"]),
            )
            raw_ic = _spearman(discovery_score, discovery_outcome)
            orientation = -1.0 if raw_ic is not None and raw_ic < 0 else 1.0
            discovery_score *= orientation
            confirmation_score *= orientation
            factor_model_rows.append({
                "factor_id": factor_id,
                "horizon_sessions": horizon,
                "discovery_raw_ic": raw_ic,
                "orientation": int(orientation),
                "discovery_observations": int(pd.concat([discovery_raw, discovery_outcome], axis=1).dropna().shape[0]),
            })
            factor_definition = registry.show(factor_id)
            result_rows.append(_audit_path(
                component_id=factor_id,
                catalog_id=factor_id,
                kind="CONTINUOUS_FACTOR",
                family=str(factor_definition["information_family"]),
                horizon=horizon,
                discovery_score=discovery_score,
                confirmation_score=confirmation_score,
                discovery_outcome=discovery_outcome,
                confirmation_outcome=confirmation_outcome,
                confirmation_residual=confirmation_residual,
                minimum_observations=int(model["minimum_confirmation_observations"]),
                annual_minimum=int(model["minimum_annual_observations"]),
                max_lags=int(statistics["hac_max_lags"]),
                metadata={"frequency": "project", "orientation": "POSITIVE" if orientation > 0 else "NEGATIVE"},
            ))

        event_definition = registry.show("F-PROJECT-STRUCTURED-NEWS-EVENT")
        result_rows.append(_audit_path(
            component_id="F-PROJECT-STRUCTURED-NEWS-EVENT",
            catalog_id="F-PROJECT-STRUCTURED-NEWS-EVENT",
            kind="EVENT_FACTOR",
            family=str(event_definition["information_family"]),
            horizon=horizon,
            discovery_score=event_score.reindex(discovery_outcome.index),
            confirmation_score=event_score.reindex(confirmation_outcome.index),
            discovery_outcome=discovery_outcome,
            confirmation_outcome=confirmation_outcome,
            confirmation_residual=confirmation_residual,
            minimum_observations=int(model["minimum_event_confirmation_observations"]),
            annual_minimum=int(model["minimum_annual_observations"]),
            max_lags=int(statistics["hac_max_lags"]),
            metadata={"frequency": "event", "orientation": "SEMANTIC_EVENT_DIRECTION"},
        ))

    results = pd.DataFrame(result_rows)
    expected = int(protocol["universe"]["preregistered_return_paths"])
    if len(results) != expected or results["path_id"].nunique() != expected:
        raise ValueError(f"full audit path count differs: {len(results)} != {expected}")
    results["horizon_bh_qvalue"] = np.nan
    for horizon, indices in results.groupby("horizon_sessions").groups.items():
        del horizon
        results.loc[indices, "horizon_bh_qvalue"] = _bh(
            results.loc[indices, "hac_one_sided_pvalue"]
        )
    results["global_bh_qvalue"] = _bh(results["hac_one_sided_pvalue"])
    stable = (
        results["identifiable"]
        & results["confirmation_ic"].gt(0)
        & results["confirmation_residual_ic"].gt(0)
        & results["positive_confirmation_years"].ge(int(statistics["minimum_positive_confirmation_years"]))
    )
    results["evidence_label"] = "NO_STABLE_EVIDENCE"
    results.loc[~results["identifiable"], "evidence_label"] = "UNIDENTIFIABLE"
    results.loc[stable, "evidence_label"] = "DIRECTIONALLY_STABLE"
    results.loc[stable & results["hac_one_sided_pvalue"].le(float(statistics["nominal_one_sided_alpha"])), "evidence_label"] = "NOMINAL_SUPPORT"
    results.loc[stable & results["global_bh_qvalue"].le(float(statistics["benjamini_hochberg_fdr"])), "evidence_label"] = "FDR_SUPPORTED"
    results = results.sort_values(
        ["evidence_label", "global_bh_qvalue", "confirmation_residual_ic"],
        ascending=[True, True, False],
    ).reset_index(drop=True)

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    results.to_csv(artifacts / "information_path_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    pd.DataFrame(state_edge_rows).to_csv(artifacts / "categorical_state_models.csv.gz", index=False, compression=compression, lineterminator="\n")
    pd.DataFrame(factor_model_rows).to_csv(artifacts / "continuous_factor_models.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    summary = (
        results.groupby(["kind", "information_family", "horizon_sessions"], as_index=False)
        .agg(
            paths=("path_id", "size"),
            identifiable=("identifiable", "sum"),
            positive_confirmation_ic=("confirmation_ic", lambda values: int(values.gt(0).sum())),
            median_confirmation_ic=("confirmation_ic", "median"),
            median_residual_ic=("confirmation_residual_ic", "median"),
        )
        .sort_values(["kind", "information_family", "horizon_sessions"])
    )
    summary.to_csv(artifacts / "family_horizon_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    supported = results.loc[results["evidence_label"].isin({"FDR_SUPPORTED", "NOMINAL_SUPPORT", "DIRECTIONALLY_STABLE"})]
    supported.to_csv(artifacts / "supported_information_paths.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    label_counts = results["evidence_label"].value_counts().sort_index()
    kind_labels = results.groupby(["kind", "evidence_label"]).size().unstack(fill_value=0)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "dataset_fingerprint": replay.fingerprint,
        "return_path_count": int(len(results)),
        "component_count": int(results["component_id"].nunique()),
        "horizons": horizons,
        "evidence_label_counts": {str(key): int(value) for key, value in label_counts.items()},
        "kind_evidence_counts": {
            str(kind): {str(label): int(value) for label, value in row.items()}
            for kind, row in kind_labels.iterrows()
        },
        "fdr_supported_count": int(results["evidence_label"].eq("FDR_SUPPORTED").sum()),
        "nominal_support_count": int(results["evidence_label"].eq("NOMINAL_SUPPORT").sum()),
        "directionally_stable_count": int(results["evidence_label"].eq("DIRECTIONALLY_STABLE").sum()),
        "unidentifiable_count": int(results["evidence_label"].eq("UNIDENTIFIABLE").sum()),
        "contextual_signals_deferred": int(protocol["universe"]["contextual_signals_deferred"]),
        "component_frequency_policy": protocol["component_frequency_policy"],
        "complete_strategy_frequency_policy": protocol["complete_strategy_frequency_policy"],
        "search_ledger_paths_added": int(len(results)),
        "decision": "PROCEED_TO_CROSS_TYPE_COMPLEMENTARITY_REVIEW" if len(supported) else "STOP_FULL_INFORMATION_AUDIT_NO_STABLE_COMPONENTS",
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "information_audit.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S006 EX06 执行\n\n"
        f"状态：`COMPLETE`。一次性完成{len(results)}条预注册路径，覆盖"
        f"{results['component_id'].nunique()}个组件和{len(horizons)}个观察周期；全部计入搜索账本。"
        "没有构建组合、生成候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S006 EX06 结论\n\n"
        f"裁决：`{evidence['decision']}`。2040条路径中，全局FDR支持"
        f"{evidence['fdr_supported_count']}条、名义支持{evidence['nominal_support_count']}条、"
        f"方向稳定{evidence['directionally_stable_count']}条、不可识别"
        f"{evidence['unidentifiable_count']}条。\n\n"
        "这些结果只衡量独立信息价值。下一步只能从机器总账读取跨类型互补性，先做去冗余和"
        "金融职责审查，再预注册有限策略结构；不得直接把单条支持路径晋升为候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
