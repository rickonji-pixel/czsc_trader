def points(count: int = 25):
    from strategy_evaluator import ParameterPoint

    rows = [
        ParameterPoint(
            "R1102", "champion", (("fast", 0.5), ("slow", 0.5)), True, 1,
            (("net_cagr", 1.0), ("max_drawdown", 1.0), ("calmar", 1.0), ("profit_factor", 1.0)),
        )
    ]
    for index in range(count):
        offset = (index + 1) / 100
        rows.append(ParameterPoint(
            f"R{index:04d}", f"behavior-{index}",
            (("fast", 0.5 + offset), ("slow", 0.5 - offset)), index != count - 1,
            1 if index < 5 else 2,
            (("net_cagr", 1.0 - offset), ("max_drawdown", 0.9 - offset),
             ("calmar", 0.8 - offset), ("profit_factor", 0.7 - offset)),
        ))
    return tuple(rows)


def test_neighborhood_uses_nearest_twenty_unique_behaviors() -> None:
    from strategy_evaluator import AuditStatus, audit_parameter_neighborhood

    result = audit_parameter_neighborhood("R1102", points(), neighbor_limit=20, minimum_valid=10)

    assert len(result.neighbors) == 20
    assert len({row.behavior_hash for row in result.neighbors}) == 20
    assert tuple(row.distance for row in result.neighbors) == tuple(
        sorted(row.distance for row in result.neighbors)
    )
    assert result.status is AuditStatus.PASS
    assert {row.metric for row in result.metrics} == {
        "net_cagr", "max_drawdown", "calmar", "profit_factor"
    }


def test_neighborhood_deduplicates_behavior_before_limiting() -> None:
    from dataclasses import replace

    from strategy_evaluator import audit_parameter_neighborhood

    source = points()
    duplicate = replace(source[1], candidate_id="duplicate", parameters=(("fast", 0.501), ("slow", 0.499)))
    result = audit_parameter_neighborhood("R1102", (*source, duplicate), neighbor_limit=20)

    behavior_rows = [row for row in result.neighbors if row.behavior_hash == source[1].behavior_hash]
    assert len(behavior_rows) == 1
    assert behavior_rows[0].candidate_id == "duplicate"


def test_neighborhood_is_insufficient_below_ten_valid_neighbors() -> None:
    from strategy_evaluator import AuditStatus, audit_parameter_neighborhood

    result = audit_parameter_neighborhood("R1102", points(9), minimum_valid=10)

    assert result.status is AuditStatus.INSUFFICIENT
    assert result.valid_neighbor_count == 9
