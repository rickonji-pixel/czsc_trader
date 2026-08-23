from czsc_trader.objectives import RETURN_TARGETS, evaluate_return, overall_pass


def test_return_equal_to_target_passes_inclusively() -> None:
    """Catch an exact target return being rejected by a strict comparison."""
    result = evaluate_return("2026Q1", 0.105)

    assert result == {"target_return": 0.105, "target_margin": 0.0, "pass": True}


def test_overall_pass_requires_all_three_absolute_targets() -> None:
    """Catch one failed window being hidden by the other two windows."""
    windows = {
        name: {"strategy_return": target, "buyhold_return": 99.0}
        for name, target in RETURN_TARGETS.items()
    }

    assert overall_pass(windows) is True
    windows["2026M1-M8"]["strategy_return"] = 0.599999
    assert overall_pass(windows) is False


def test_buyhold_return_cannot_change_absolute_pass() -> None:
    """Catch PASS reverting to a strategy-versus-benchmark comparison."""
    low_benchmark = evaluate_return("2026H1", 0.825)
    high_benchmark = evaluate_return("2026H1", 0.825)

    assert low_benchmark["pass"] is high_benchmark["pass"] is True
