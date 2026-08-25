"""Causal trajectory anatomy for dominant false exits of the 0824_EX04 strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
import json

import numpy as np
import pandas as pd

from .factors import signal_primary
from .baselines import resolve_baseline
from .data import load_market_data
from .exit_signal_diagnosis_runner import validate_source_archive
from .ex04_path_attribution_runner import validate_artifact_identity
from .factors import generate_factor_frame
from .four_layer import score_four_layer, validate_fixed_factor_weights
from .four_layer_runner import _equivalence


_DESCRIPTOR_LISTS = {
    "run_length_bins": (1, 3, 5, 10, 20),
    "transition_age_bins": (1, 3, 5, 10, 20),
    "flip_windows": (5, 10, 20),
    "score_lags": (1, 3, 5),
    "below_threshold_windows": (5, 10),
}


def validate_protocol(protocol: Mapping[str, object]) -> None:
    """Reject drift from the preregistered 0825_EX05 diagnostic boundary."""
    if protocol.get("experiment_type") != "dominant_exit_path_anatomy":
        raise ValueError("not a dominant exit anatomy protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol status must remain PRE_REGISTERED")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("visible sample must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("holdout access must remain disabled")
    if int(protocol.get("expected_event_count", -1)) != 20:
        raise ValueError("anatomy requires exactly 20 events")
    if int(protocol.get("expected_factor_count", -1)) != 12:
        raise ValueError("anatomy requires exactly 12 frozen factors")
    if int(protocol.get("lookback_trading_days", -1)) != 20:
        raise ValueError("anatomy lookback must remain 20 trading days")
    discovery = tuple(str(value) for value in protocol.get("discovery_event_ids", ()))
    confirmation = str(protocol.get("confirmation_event_id", ""))
    if len(discovery) != 2 or len(set(discovery)) != 2 or confirmation in discovery:
        raise ValueError("discovery and confirmation event identities are invalid")
    for key, expected in _DESCRIPTOR_LISTS.items():
        if tuple(int(value) for value in protocol.get(key, ())) != expected:
            raise ValueError(f"descriptor definition drifted: {key}")
    if int(protocol.get("maximum_protective_support", -1)) != 2:
        raise ValueError("protective contamination threshold drifted")
    if protocol.get("require_zero_joint_margin_protective_support") is not True:
        raise ValueError("joint-margin protective exclusion drifted")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping) or any(bool(value) for value in promotion.values()):
        raise ValueError("promotion and optimization must remain disabled")


def validate_event_cohort(events: pd.DataFrame, protocol: Mapping[str, object]) -> None:
    """Require the exact tracked 0825_EX04 cohort and fixed dominant events."""
    required = {"event_id", "signal_date", "outcome_label", "block_label"}
    if not required <= set(events.columns):
        raise ValueError("event cohort columns are incomplete")
    ids = events["event_id"].astype(str)
    if len(events) != int(protocol["expected_event_count"]) or ids.duplicated().any():
        raise ValueError("event cohort must contain exactly 20 unique events")
    dominant = {
        *(str(value) for value in protocol["discovery_event_ids"]),
        str(protocol["confirmation_event_id"]),
    }
    if not dominant <= set(ids):
        raise ValueError("fixed dominant event identity differs from preregistration")
    if events["signal_date"].astype(str).str.contains("2026", regex=False).any():
        raise ValueError("event cohort contains a 2026 date")
    allowed = {"false_exit", "protective_exit", "neutral_exit"}
    if not set(events["outcome_label"].astype(str)) <= allowed:
        raise ValueError("event cohort contains an unknown outcome label")


def validate_visible_hashes(hashes: Mapping[str, str]) -> None:
    """Reject any metadata evidence that a holdout file was loaded."""
    if any("2026" in str(name) for name in hashes):
        raise AssertionError("2026 file entered dominant exit anatomy inputs")


def validate_trajectory_evidence(
    trajectory: pd.DataFrame,
    events: pd.DataFrame,
    factor_names: Sequence[str],
    protocol: Mapping[str, object],
) -> None:
    """Audit causal bounds, factor identity, and source-score equivalence."""
    required = {
        "event_id", "date", "signal_date", "offset", "ex04_score", "source_ex04_score"
    } | {f"mapped::{name}" for name in factor_names}
    if not required <= set(trajectory.columns):
        raise ValueError("trajectory evidence columns are incomplete")
    if len(factor_names) != int(protocol["expected_factor_count"]):
        raise ValueError("trajectory factor count differs from preregistration")
    event_ids = events["event_id"].astype(str)
    if set(trajectory["event_id"].astype(str)) != set(event_ids):
        raise ValueError("trajectory and event identities differ")
    frame = trajectory.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["signal_date"] = pd.to_datetime(frame["signal_date"])
    if frame["date"].gt(frame["signal_date"]).any() or frame["offset"].astype(int).gt(0).any():
        raise ValueError("trajectory contains future rows after the signal date")
    counts = frame.groupby(frame["event_id"].astype(str)).size()
    if counts.gt(int(protocol["lookback_trading_days"]) + 1).any():
        raise ValueError("trajectory exceeds the preregistered lookback")
    terminal = frame.loc[frame["offset"].astype(int).eq(0)]
    if terminal["event_id"].astype(str).value_counts().ne(1).any() or len(terminal) != len(events):
        raise ValueError("each event must have exactly one signal-day trajectory row")
    score_error = (
        terminal["ex04_score"].astype(float) - terminal["source_ex04_score"].astype(float)
    ).abs()
    if score_error.max() > 1e-12:
        raise AssertionError("regenerated 0824_EX04 score differs from 0825_EX04 source score")
    numeric = frame.select_dtypes(include=[np.number])
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError("trajectory contains non-finite values")


def render_dominant_event_svg(
    trajectory: pd.DataFrame,
    event_ids: Sequence[str],
    *,
    exit_threshold: float,
    titles: Sequence[str],
) -> str:
    """Render three portable causal panels without post-signal observations."""
    if len(event_ids) != 3 or len(titles) != 3:
        raise ValueError("dominant event SVG requires exactly three panels")
    width, panel_height, margin = 960, 220, 48
    height = panel_height * 3
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Segoe UI,Arial;font-size:12px}.price{fill:none;stroke:#1f77b4;stroke-width:2}'
        '.score{fill:none;stroke:#d62728;stroke-width:2}.threshold{stroke:#777;stroke-dasharray:5 4}'
        '.signal-day{stroke:#111;stroke-dasharray:2 3}</style>',
    ]
    for panel, (event_id, title) in enumerate(zip(event_ids, titles, strict=True)):
        selected = trajectory.loc[trajectory["event_id"].astype(str).eq(str(event_id))].sort_values(
            "offset"
        )
        if selected.empty or selected["offset"].astype(int).gt(0).any():
            raise ValueError("SVG trajectory is missing or contains future rows")
        x0, x1 = margin, width - margin
        y0 = panel * panel_height + 36
        chart_height = panel_height - 66
        offsets = selected["offset"].astype(float).to_numpy()
        xmin, xmax = float(offsets.min()), float(offsets.max())
        xspan = xmax - xmin or 1.0
        xs = x0 + (offsets - xmin) / xspan * (x1 - x0)

        def points(values: pd.Series) -> str:
            array = values.astype(float).to_numpy()
            low, high = float(array.min()), float(array.max())
            span = high - low or 1.0
            ys = y0 + chart_height - (array - low) / span * chart_height
            return " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys, strict=True))

        score = selected["ex04_score"].astype(float)
        score_low = min(float(score.min()), float(exit_threshold))
        score_high = max(float(score.max()), float(exit_threshold))
        score_span = score_high - score_low or 1.0
        threshold_y = y0 + chart_height - (float(exit_threshold) - score_low) / score_span * chart_height
        signal_x = float(xs[-1])
        pieces.extend(
            [
                f'<text x="{margin}" y="{panel * panel_height + 20}">{escape(str(title))}</text>',
                f'<polyline class="price" points="{points(selected["normalized_close"])}"/>',
                f'<polyline class="score" points="{points(score)}"/>',
                f'<line class="threshold" x1="{x0}" x2="{x1}" y1="{threshold_y:.2f}" y2="{threshold_y:.2f}"/>',
                f'<line class="signal-day" x1="{signal_x:.2f}" x2="{signal_x:.2f}" y1="{y0}" y2="{y0 + chart_height}"/>',
                f'<text x="{x0}" y="{y0 + chart_height + 18}">T-20</text>',
                f'<text x="{x1 - 12}" y="{y0 + chart_height + 18}">T</text>',
            ]
        )
    pieces.append("</svg>\n")
    return "".join(pieces)


def _sign(value: float, *, tolerance: float = 1e-15) -> str:
    number = float(value)
    if number > tolerance:
        return "positive"
    if number < -tolerance:
        return "negative"
    return "zero"


def _bounded_bin(value: int, bounds: Sequence[int], *, none: bool = False) -> str:
    number = int(value)
    if none and number < 0:
        return "none"
    lower = 1 if not none else 0
    for bound in bounds:
        if number <= int(bound):
            if lower == int(bound):
                return str(bound)
            return f"{lower}_{int(bound)}"
        lower = int(bound) + 1
    return f"{lower}_plus"


def _count_bin(value: int) -> str:
    number = int(value)
    if number <= 1:
        return str(number)
    return "2_plus"


def _primary_series(series: pd.Series) -> pd.Series:
    return series.map(lambda value: signal_primary(value) or "__missing__").astype(str)


def _transition_descriptors(states: pd.Series) -> tuple[int, str, int]:
    values = states.astype(str).tolist()
    run_length = 1
    for value in reversed(values[:-1]):
        if value != values[-1]:
            break
        run_length += 1
    transitions = [index for index in range(1, len(values)) if values[index] != values[index - 1]]
    if not transitions:
        return run_length, "no_transition", -1
    last = transitions[-1]
    return run_length, f"{values[last - 1]}=>{values[last]}", len(values) - 1 - last


def extract_signal_descriptors(
    raw: pd.DataFrame,
    mapped: pd.DataFrame,
    contributions: pd.DataFrame,
    score: pd.Series,
    signal_date: pd.Timestamp,
    protocol: Mapping[str, object],
    *,
    exit_threshold: float,
    direction_factors: Sequence[str],
) -> dict[str, object]:
    """Build the frozen atomic descriptors using no row after ``signal_date``."""
    date = pd.Timestamp(signal_date)
    if date not in raw.index:
        raise ValueError("signal date is absent from factor frame")
    if not raw.index.equals(mapped.index) or not raw.index.equals(contributions.index):
        raise ValueError("raw, mapped, and contribution indices differ")
    if tuple(raw.columns) != tuple(mapped.columns) or tuple(raw.columns) != tuple(
        contributions.columns
    ):
        raise ValueError("raw, mapped, and contribution factor identities differ")
    if not raw.index.equals(score.index):
        raise ValueError("score index differs from factor frame")
    lookback = int(protocol["lookback_trading_days"])
    history_raw = raw.loc[raw.index <= date].tail(lookback + 1)
    history_mapped = mapped.loc[history_raw.index]
    history_contributions = contributions.loc[history_raw.index]
    history_score = score.loc[history_raw.index].astype(float)
    if history_raw.empty or pd.Timestamp(history_raw.index[-1]) != date:
        raise ValueError("causal trajectory does not end on signal date")
    numeric = pd.concat([history_mapped, history_contributions], axis=1)
    if not np.isfinite(numeric.to_numpy(dtype=float)).all() or not np.isfinite(
        history_score.to_numpy(dtype=float)
    ).all():
        raise ValueError("trajectory contains non-finite values")

    output: dict[str, object] = {}
    for factor in raw.columns:
        states = _primary_series(history_raw[factor])
        run_length, transition, transition_age = _transition_descriptors(states)
        output[f"{factor}|primary_at_t"] = states.iloc[-1]
        output[f"{factor}|mapped_at_t"] = _sign(history_mapped[factor].iloc[-1])
        output[f"{factor}|run_length_bin"] = _bounded_bin(
            run_length, protocol["run_length_bins"]
        )
        output[f"{factor}|last_transition"] = transition
        output[f"{factor}|transition_age_bin"] = _bounded_bin(
            transition_age, protocol["transition_age_bins"], none=True
        )
        for window in protocol["flip_windows"]:
            window = int(window)
            recent = states.tail(window + 1)
            flips = int(recent.ne(recent.shift()).iloc[1:].sum())
            output[f"{factor}|flips_{window}_bin"] = _count_bin(flips)
        for lag in (1, 5):
            delta = (
                float(history_contributions[factor].iloc[-1])
                - float(history_contributions[factor].iloc[-lag - 1])
                if len(history_contributions) > lag
                else 0.0
            )
            output[f"{factor}|contribution_delta_{lag}_sign"] = _sign(delta)

    for lag in protocol["score_lags"]:
        lag = int(lag)
        delta = (
            float(history_score.iloc[-1]) - float(history_score.iloc[-lag - 1])
            if len(history_score) > lag
            else 0.0
        )
        output[f"__strategy__|score_delta_{lag}_sign"] = _sign(delta)
    output["__strategy__|exit_margin_sign"] = _sign(
        float(history_score.iloc[-1]) - float(exit_threshold)
    )
    for window in protocol["below_threshold_windows"]:
        window = int(window)
        count = int(history_score.tail(window).le(float(exit_threshold)).sum())
        output[f"__strategy__|below_exit_{window}_bin"] = _bounded_bin(
            count, (1, 3, 5, 10)
        ) if count else "0"

    missing_directions = [name for name in direction_factors if name not in history_mapped]
    if missing_directions:
        raise ValueError(f"direction factors are missing: {missing_directions}")
    directions = [
        _sign(history_mapped[name].iloc[-1]) for name in direction_factors
    ]
    counts = pd.Series(directions).value_counts()
    if len(directions) == 3 and len(counts) == 1 and directions[0] != "zero":
        alignment = "all_same"
    elif len(directions) == 3 and int(counts.max()) == 2 and counts.index[0] != "zero":
        alignment = "two_same"
    else:
        alignment = "all_different_or_neutral"
    output["__strategy__|direction_alignment"] = alignment
    return output


def discover_atomic_signatures(
    matrix: pd.DataFrame,
    events: pd.DataFrame,
    protocol: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Discover on the two 2021 events, then confirm once on the 2023 event."""
    required = {"event_id", "outcome_label", "block_label"}
    if not required <= set(events.columns):
        raise ValueError("event metadata columns are incomplete")
    event_ids = events["event_id"].astype(str)
    if len(event_ids) != int(protocol["expected_event_count"]) or event_ids.duplicated().any():
        raise ValueError("event identities differ from the preregistered cohort")
    if set(matrix.index.astype(str)) != set(event_ids):
        raise ValueError("descriptor matrix and event identities differ")
    matrix = matrix.copy()
    matrix.index = matrix.index.astype(str)
    metadata = events.copy().set_index(events["event_id"].astype(str))
    discovery = tuple(str(value) for value in protocol["discovery_event_ids"])
    confirmation_id = str(protocol["confirmation_event_id"])
    if not set((*discovery, confirmation_id)) <= set(matrix.index):
        raise ValueError("fixed dominant events are missing")

    rows: list[dict[str, object]] = []
    for column in matrix.columns:
        if "|" not in str(column):
            raise ValueError(f"descriptor column lacks factor boundary: {column}")
        values = matrix[column].astype("string")
        if values.nunique(dropna=False) <= 1:
            continue
        left, right = values.loc[discovery[0]], values.loc[discovery[1]]
        if pd.isna(left) or pd.isna(right) or str(left) != str(right):
            continue
        value = str(left)
        support = values.eq(value)
        protective = metadata["outcome_label"].astype(str).eq("protective_exit")
        joint = metadata["block_label"].astype(str).eq("joint_margin_block")
        false = metadata["outcome_label"].astype(str).eq("false_exit")
        neutral = metadata["outcome_label"].astype(str).eq("neutral_exit")
        confirmed = bool(support.loc[confirmation_id])
        protective_support = int((support & protective).sum())
        joint_protective_support = int((support & protective & joint).sum())
        low = bool(
            confirmed
            and protective_support <= int(protocol["maximum_protective_support"])
            and (
                not bool(protocol["require_zero_joint_margin_protective_support"])
                or joint_protective_support == 0
            )
        )
        factor, descriptor = str(column).split("|", 1)
        rows.append(
            {
                "factor": factor,
                "descriptor": descriptor,
                "value": value,
                "discovery_support": 2,
                "confirmation_support": int(confirmed),
                "total_support": int(support.sum()),
                "protective_support": protective_support,
                "joint_margin_protective_support": joint_protective_support,
                "other_false_support": int(
                    (support & false).sum() - support.loc[list(discovery)].sum() - int(support.loc[confirmation_id])
                ),
                "neutral_support": int((support & neutral).sum()),
                "confirmed": confirmed,
                "low_contamination": low,
            }
        )
    columns = [
        "factor", "descriptor", "value", "discovery_support", "confirmation_support",
        "total_support", "protective_support", "joint_margin_protective_support",
        "other_false_support", "neutral_support", "confirmed", "low_contamination",
    ]
    discovered = pd.DataFrame(rows, columns=columns).sort_values(
        ["factor", "descriptor", "value"], ignore_index=True
    )
    confirmed = discovered.loc[discovered["confirmed"].astype(bool)].reset_index(drop=True)
    return discovered, confirmed


def classify_anatomy(
    events: pd.DataFrame,
    discovered: pd.DataFrame,
    confirmed: pd.DataFrame,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Apply the frozen 0825_EX05 anatomy classification order."""
    validate_protocol(protocol)
    if len(events) != int(protocol["expected_event_count"]):
        classification = "insufficient_anatomy_evidence"
    elif not confirmed.empty and confirmed.get(
        "low_contamination", pd.Series(dtype=bool)
    ).astype(bool).any():
        classification = "shared_confirmed_low_contamination_anatomy"
    elif not confirmed.empty:
        classification = "shared_but_contaminated_anatomy"
    elif not discovered.empty:
        classification = "discovery_only_anatomy"
    else:
        classification = "idiosyncratic_dominant_events"
    return {
        "classification": classification,
        "event_count": len(events),
        "discovered_signature_count": len(discovered),
        "confirmed_signature_count": len(confirmed),
        "low_contamination_signature_count": int(
            confirmed.get("low_contamination", pd.Series(dtype=bool)).astype(bool).sum()
        ),
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def run_dominant_exit_anatomy(
    raw_dir: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Run the preregistered causal anatomy without strategy optimization or holdout."""
    validate_protocol(protocol)
    experiment_dir = Path(experiment_dir).resolve()
    repository_root = experiment_dir.parent.parent
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    for forbidden in ("frozen_challenger.json", "orders.csv", "candidate_results.csv"):
        if (artifacts / forbidden).exists():
            raise ValueError(f"anatomy archive contains forbidden {forbidden}")

    source = protocol.get("source_archive")
    if not isinstance(source, Mapping):
        raise ValueError("0825_EX04 source archive identity is missing")
    source_manifest = validate_source_archive(repository_root, source)
    source_dir = repository_root / str(source["path"])

    general = protocol.get("general_baseline")
    if not isinstance(general, Mapping):
        raise ValueError("general baseline identity is missing")
    baseline = resolve_baseline(
        repository_root / "configs" / "rule_baselines", str(general["version"])
    )
    if baseline.sha256 != str(general["sha256"]):
        raise ValueError("general baseline hash differs from preregistration")

    research = protocol.get("research_baseline")
    if not isinstance(research, Mapping):
        raise ValueError("0824_EX04 research baseline identity is missing")
    frozen_path = repository_root / str(research["path"])
    frozen_digest = validate_artifact_identity(
        frozen_path, str(research["sha256"]), label="0824_EX04"
    )
    frozen = json.loads(frozen_path.read_text(encoding="utf-8-sig"))

    cutoff = pd.Timestamp(str(protocol["visible_sample_end"]))
    data = load_market_data(
        Path(raw_dir), str(protocol["symbol"]), str(protocol["asset_type"]), cutoff=cutoff
    )
    validate_visible_hashes(data.hashes)
    factor_result = generate_factor_frame(data)
    factor_frame = factor_result.frame
    mapped, _, equivalence = _equivalence(
        factor_frame, baseline.rule, 1e-12
    )
    factor_names = tuple(str(name) for name in frozen["factor_names"])
    if len(factor_names) != int(protocol["expected_factor_count"]) or factor_names != tuple(
        mapped.columns
    ):
        raise ValueError("0824_EX04 factor identities differ from preregistration")
    weights = pd.Series(
        [float(frozen["weights"][name]) for name in factor_names],
        index=mapped.columns,
        name="weight",
    )
    validate_fixed_factor_weights(weights, mapped.columns, 0.0)
    contributions = mapped.mul(weights, axis=1)
    score = score_four_layer(mapped, weights)
    raw = factor_frame.loc[:, list(factor_names)].copy()
    if not raw.index.equals(mapped.index):
        raise ValueError("raw and mapped factor frames are not aligned")

    events = pd.read_csv(source_dir / "artifacts" / "exit_event_counterfactuals.csv")
    validate_event_cohort(events, protocol)
    events["signal_date"] = pd.to_datetime(events["signal_date"])
    if events["signal_date"].max() > cutoff:
        raise ValueError("event cohort exceeds visible sample cutoff")
    prices = data.daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    prices = prices.sort_index()

    direction_factors = tuple(name for name in factor_names if "cxt_bi_status" in name)
    if len(direction_factors) != 3:
        raise ValueError("expected 30m, daily, and weekly direction factors")
    exit_threshold = float(frozen["spec"]["exit"])
    trajectory_rows: list[dict[str, object]] = []
    descriptor_rows: list[dict[str, object]] = []
    for event in events.to_dict(orient="records"):
        event_id = str(event["event_id"])
        signal_date = pd.Timestamp(event["signal_date"])
        history_index = mapped.index[mapped.index <= signal_date][
            -(int(protocol["lookback_trading_days"]) + 1) :
        ]
        if len(history_index) == 0 or pd.Timestamp(history_index[-1]) != signal_date:
            raise ValueError(f"{event_id}: signal date is absent from causal factor history")
        start_close = float(prices.loc[history_index[0], "close"])
        offsets = range(-(len(history_index) - 1), 1)
        for offset, date in zip(offsets, history_index, strict=True):
            row: dict[str, object] = {
                "event_id": event_id,
                "window": str(event["window"]),
                "outcome_label": str(event["outcome_label"]),
                "block_label": str(event["block_label"]),
                "signal_date": signal_date,
                "date": pd.Timestamp(date),
                "offset": int(offset),
                "normalized_close": float(prices.loc[date, "close"]) / start_close,
                "ex04_score": float(score.loc[date]),
                "source_ex04_score": float(event["ex04_score"]),
            }
            for factor in factor_names:
                row[f"raw::{factor}"] = signal_primary(raw.loc[date, factor]) or "__missing__"
                row[f"mapped::{factor}"] = float(mapped.loc[date, factor])
                row[f"contribution::{factor}"] = float(contributions.loc[date, factor])
            trajectory_rows.append(row)
        descriptors = extract_signal_descriptors(
            raw,
            mapped,
            contributions,
            score,
            signal_date,
            protocol,
            exit_threshold=exit_threshold,
            direction_factors=direction_factors,
        )
        descriptor_rows.append(
            {
                "event_id": event_id,
                "window": str(event["window"]),
                "signal_date": signal_date,
                "outcome_label": str(event["outcome_label"]),
                "block_label": str(event["block_label"]),
                **descriptors,
            }
        )

    trajectory = pd.DataFrame(trajectory_rows)
    descriptor_frame = pd.DataFrame(descriptor_rows)
    validate_trajectory_evidence(trajectory, events, factor_names, protocol)
    metadata_columns = {"event_id", "window", "signal_date", "outcome_label", "block_label"}
    descriptor_columns = [
        column for column in descriptor_frame.columns if column not in metadata_columns
    ]
    descriptor_matrix = descriptor_frame.set_index("event_id").loc[:, descriptor_columns]
    discovered, confirmed = discover_atomic_signatures(
        descriptor_matrix, events, protocol
    )
    classification = classify_anatomy(events, discovered, confirmed, protocol)

    dominant_ids = (
        *(str(value) for value in protocol["discovery_event_ids"]),
        str(protocol["confirmation_event_id"]),
    )
    svg = render_dominant_event_svg(
        trajectory,
        dominant_ids,
        exit_threshold=exit_threshold,
        titles=(
            "0825_EX04病灶 2021-06-16（发现）",
            "0825_EX04病灶 2021-07-06（发现）",
            "0825_EX04病灶 2023-02-08（确认）",
        ),
    )
    signal_day = trajectory.loc[trajectory["offset"].astype(int).eq(0)]
    max_score_error = float(
        (signal_day["ex04_score"].astype(float) - signal_day["source_ex04_score"].astype(float))
        .abs()
        .max()
    )
    identity = {
        "status": "PASS",
        "research_baseline": {
            "experiment_id": str(research["experiment_id"]),
            "path": str(research["path"]),
            "sha256": frozen_digest,
        },
        "source_archive": {
            "experiment_id": source_manifest["experiment_id"],
            "status": source_manifest["status"],
            "holdout_accessed": source_manifest["holdout_accessed"],
            "file_sha256": dict(source["files"]),
        },
        "general_baseline": {"version": baseline.version, "sha256": baseline.sha256},
        "factor_equivalence": equivalence,
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
    }
    metrics = {
        "status": "COMPLETE",
        "experiment_id": str(protocol["experiment_id"]),
        "visible_sample_end": str(cutoff.date()),
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
        "frozen_challenger": None,
        "event_count": len(events),
        "factor_count": len(factor_names),
        "trajectory_rows": len(trajectory),
        "descriptor_count": len(descriptor_columns),
        "discovered_signature_count": len(discovered),
        "confirmed_signature_count": len(confirmed),
        "low_contamination_signature_count": int(
            confirmed.get("low_contamination", pd.Series(dtype=bool)).astype(bool).sum()
        ),
        "max_signal_day_score_error": max_score_error,
        "anatomy_classification": classification,
    }
    _write_csv(artifacts / "event_signal_trajectory.csv", trajectory)
    _write_csv(artifacts / "event_descriptor_matrix.csv", descriptor_frame)
    _write_csv(artifacts / "discovered_atomic_signatures.csv", discovered)
    _write_csv(artifacts / "confirmed_atomic_signatures.csv", confirmed)
    _write_json(artifacts / "anatomy_classification.json", classification)
    (artifacts / "dominant_event_panels.svg").write_text(svg, encoding="utf-8")
    _write_json(artifacts / "identity_audit.json", identity)
    _write_json(artifacts / "metrics.json", metrics)
    return metrics
