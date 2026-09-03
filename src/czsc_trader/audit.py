"""Explicit temporal assertions for the research output."""

from __future__ import annotations

import numpy as np
import pandas as pd


def audit_candidate_evaluation(
    period_results: dict[str, object],
    target_position: pd.Series,
    factor_events: pd.DataFrame,
    scores: pd.Series,
) -> dict[str, int | str]:
    """Audit evaluation orders once per candidate without building provenance frames."""
    target = target_position.astype(float)
    if target.isna().any() or not np.isfinite(target).all() or not target.between(0.0, 1.0).all():
        raise AssertionError("target_position contains values outside [0, 1]")
    index = pd.DatetimeIndex(pd.to_datetime(target.index), name="dt")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise AssertionError("target_position index must be unique and increasing")
    if not target.index.equals(scores.index):
        raise AssertionError("factor scores differ from target index")
    target_values = target.to_numpy()
    score_values = scores.astype(float).to_numpy()
    locations = {timestamp: location for location, timestamp in enumerate(index)}

    event_map: dict[tuple[pd.Timestamp, str], list[dict[str, object]]] = {}
    for event in factor_events.to_dict("records"):
        key = (pd.Timestamp(event["signal_date"]), str(event["event_type"]))
        event_map.setdefault(key, []).append(event)

    orders_checked = 0
    expected_by_side = {"Buy": ("Entry", "Increase"), "Sell": ("Reduce", "Exit")}
    for result in period_results.values():
        orders = result.orders
        if orders.empty:
            continue
        period_start = pd.Timestamp(result.metrics["start"])
        for order in orders.itertuples(index=False):
            orders_checked += 1
            signal_date = pd.Timestamp(order.signal_date)
            execution_date = pd.Timestamp(order.execution_date)
            side = str(order.side)
            location = locations.get(signal_date, -1)
            if location < 0 or location + 1 >= len(index):
                raise AssertionError("order signal date has no next trading date")
            if not signal_date < execution_date or execution_date != index[location + 1]:
                raise AssertionError("every order must execute on the next trading date")
            before = 0.0 if location == 0 else float(target_values[location - 1])
            after = float(target_values[location])
            initial = execution_date == period_start and signal_date < period_start and side == "Buy"
            if initial:
                if after <= 0.0:
                    raise AssertionError("initial entry does not match an active factor target")
                continue
            expected_types = expected_by_side.get(side, ())
            matches = [
                event
                for event_type in expected_types
                for event in event_map.get((signal_date, event_type), ())
            ]
            if len(matches) != 1:
                raise AssertionError("order must reference exactly one factor event")
            event = matches[0]
            event_type = str(event["event_type"])
            valid_transition = (
                (event_type == "Entry" and before == 0.0 and after > 0.0)
                or (event_type == "Exit" and before > 0.0 and after == 0.0)
                or (event_type == "Reduce" and before > after > 0.0)
                or (event_type == "Increase" and 0.0 < before < after)
            )
            if not valid_transition:
                raise AssertionError("factor event does not match target transition")
            if (
                abs(float(event["before_position"]) - before) > 1e-12
                or abs(float(event["after_position"]) - after) > 1e-12
            ):
                raise AssertionError("factor event positions differ from target transition")
            if "factor_score" in event:
                if abs(float(event["factor_score"]) - float(score_values[location])) > 1e-12:
                    raise AssertionError("factor event score differs from candidate score")
    return {
        "status": "PASS",
        "windows_checked": len(period_results),
        "orders_checked": orders_checked,
        "positions_checked": len(target),
    }


def audit_no_lookahead(
    orders: pd.DataFrame,
    factor_events: pd.DataFrame,
    target_position: pd.Series,
    factor_frame: pd.DataFrame,
) -> dict[str, int | str]:
    """Reject trades that lack exact CZSC factor provenance or causal timing."""
    target_values = target_position.astype(float)
    if (
        target_values.isna().any()
        or not np.isfinite(target_values).all()
        or not target_values.between(0.0, 1.0).all()
    ):
        raise AssertionError("target_position contains values outside [0, 1]")
    if not target_position.index.is_monotonic_increasing or target_position.index.has_duplicates:
        raise AssertionError("target_position index must be unique and increasing")

    if not orders.empty:
        signal_dates = pd.to_datetime(orders["signal_date"])
        execution_dates = pd.to_datetime(orders["execution_date"])
        if signal_dates.isna().any() or not (signal_dates < execution_dates).all():
            raise AssertionError("every signal_date must be strictly before execution_date")
        required = {"factor_event_id", "event_type", "side"}
        if not required <= set(orders.columns) or orders["factor_event_id"].isna().any():
            raise AssertionError("every order must reference a factor event")

    events = factor_events.copy()
    if not events.empty:
        events["signal_date"] = pd.to_datetime(events["signal_date"])
    frame = factor_frame.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="dt")
    target = target_position.copy()
    target.index = pd.DatetimeIndex(pd.to_datetime(target.index), name="dt")
    previous = target.shift(1, fill_value=0.0)

    for _, order in orders.iterrows():
        event_id = str(order["factor_event_id"])
        matches = events.loc[events["event_id"] == event_id] if not events.empty else events
        if len(matches) != 1:
            raise AssertionError(f"order must reference exactly one factor event: {event_id}")
        event = matches.iloc[0]
        signal_date = pd.Timestamp(order["signal_date"])
        signal_location = target.index.get_indexer([signal_date])[0]
        if signal_location < 0 or signal_location + 1 >= len(target.index):
            raise AssertionError("order signal date has no next trading date")
        expected_execution = target.index[signal_location + 1]
        if pd.Timestamp(order["execution_date"]) != expected_execution:
            raise AssertionError("every order must execute on the next trading date")
        if pd.Timestamp(event["signal_date"]) != signal_date:
            raise AssertionError("order signal_date differs from factor event")
        event_type = str(order["event_type"])
        expected_types = {
            "Buy": {"Entry", "Increase", "InitialEntry"},
            "Sell": {"Reduce", "Exit"},
        }
        if event_type not in expected_types.get(str(order["side"]), set()) or event_type != str(event["event_type"]):
            raise AssertionError("order direction does not match factor event direction")
        if signal_date not in target.index or signal_date not in frame.index:
            raise AssertionError("factor event date is missing from target or factor frame")
        before = float(previous.loc[signal_date])
        after = float(target.loc[signal_date])
        if event_type == "Entry" and not (before == 0.0 and after > 0.0):
            raise AssertionError("entry factor event does not match a target transition")
        if event_type == "Exit" and not (before > 0.0 and after == 0.0):
            raise AssertionError("exit factor event does not match a target transition")
        if event_type == "Reduce" and not (before > after > 0.0):
            raise AssertionError("reduce factor event does not match a target transition")
        if event_type == "Increase" and not (0.0 < before < after):
            raise AssertionError("increase factor event does not match a target transition")
        if event_type == "InitialEntry" and after <= 0.0:
            raise AssertionError("initial entry does not match an active factor target")
        expected_before = 0.0 if event_type == "InitialEntry" else before
        if (
            "before_position" not in event
            or "after_position" not in event
            or abs(float(event["before_position"]) - expected_before) > 1e-12
            or abs(float(event["after_position"]) - after) > 1e-12
        ):
            raise AssertionError("factor event positions differ from target transition")
        if "factor_score" in event and "factor_score" in frame:
            if abs(float(event["factor_score"]) - float(frame.loc[signal_date, "factor_score"])) > 1e-12:
                raise AssertionError("factor event score differs from factor frame")

    return {
        "status": "PASS",
        "orders_checked": int(len(orders)),
        "events_checked": int(orders["factor_event_id"].nunique()) if not orders.empty else 0,
        "positions_checked": int(len(target_position)),
    }
