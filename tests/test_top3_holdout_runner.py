from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

import czsc_trader.data as data_module


PROTOCOL_PATH = Path("experiments/0825_EX01/artifacts/protocol.json")
RANKING_PATH = Path("experiments/0824_EX08/artifacts/trial_ranking.csv")


def test_preregistered_rules_select_exact_top3_and_eight_unique_trials() -> None:
    spec = importlib.util.find_spec("czsc_trader.top3_holdout_runner")
    assert spec is not None, "formal Top3 holdout runner is missing"

    from czsc_trader.top3_holdout_runner import select_rule_top3

    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    ranking = pd.read_csv(RANKING_PATH, encoding="utf-8-sig")

    selected = select_rule_top3(ranking, protocol)

    assert selected == {
        "robustness_first": (3123, 2806, 629),
        "win_count_first": (2385, 2806, 2478),
        "mean_return_first": (3384, 1192, 2214),
    }
    assert tuple(sorted({trial for values in selected.values() for trial in values})) == (
        629,
        1192,
        2214,
        2385,
        2478,
        2806,
        3123,
        3384,
    )


def test_protocol_rejects_any_changed_rule_or_trial() -> None:
    from czsc_trader.top3_holdout_runner import validate_tournament_protocol

    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    validate_tournament_protocol(protocol)

    changed = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    changed["rules"]["mean_return_first"]["top_trials"] = [3384, 1192, 3123]
    with pytest.raises(ValueError, match="protocol"):
        validate_tournament_protocol(changed)


def test_freeze_reconstructs_eight_trials_without_opening_2026(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import czsc_trader.top3_holdout_runner as runner

    freeze_tournament_candidates = getattr(runner, "freeze_tournament_candidates", None)
    assert freeze_tournament_candidates is not None, "freeze phase is missing"

    experiment = tmp_path / "0825_EX01"
    artifacts = experiment / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "protocol.json").write_bytes(PROTOCOL_PATH.read_bytes())
    opened: list[str] = []
    original = data_module._read_one

    def recording_read(path: Path, freq: str, symbol: str) -> pd.DataFrame:
        opened.append(path.name)
        return original(path, freq, symbol)

    monkeypatch.setattr(data_module, "_read_one", recording_read)

    frozen, digest = freeze_tournament_candidates(Path.cwd(), experiment)

    assert tuple(candidate["trial_number"] for candidate in frozen["candidates"]) == (
        629,
        1192,
        2214,
        2385,
        2478,
        2806,
        3123,
        3384,
    )
    assert len(digest) == 64
    assert (artifacts / "selection_rule_top3.csv").is_file()
    assert (artifacts / "frozen_candidates.json").is_file()
    assert opened
    assert all("_2026.csv" not in name for name in opened)


def test_candidate_pass_requires_strict_return_win_in_every_window() -> None:
    import czsc_trader.top3_holdout_runner as runner

    candidate_window = getattr(runner, "candidate_window", None)
    assert candidate_window is not None, "holdout comparison is missing"
    baseline = {
        "strategy_return": 0.10,
        "sharpe": 1.0,
        "max_drawdown": -0.1,
        "exposure": 0.5,
        "trade_count": 2,
    }

    win = candidate_window(
        baseline, {**baseline, "strategy_return": 0.11, "sharpe": 0.5}
    )
    tie = candidate_window(
        baseline, {**baseline, "strategy_return": 0.10, "sharpe": 2.0}
    )

    assert win["pass"] is True
    assert win["return_delta_vs_ex04"] == pytest.approx(0.01)
    assert tie["pass"] is False
    assert tie["sharpe_delta_vs_ex04"] == pytest.approx(1.0)
