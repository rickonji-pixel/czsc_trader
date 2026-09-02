from pathlib import Path

import pytest


def test_service_config_round_trips_and_builds_serve_arguments(tmp_path: Path) -> None:
    from paper_trading_engine.service_config import ServiceConfig

    config = ServiceConfig(repo_root=tmp_path.resolve())
    path = tmp_path / "state" / "paper_trading" / "service.json"
    config.save(path)
    loaded = ServiceConfig.load(path)

    assert loaded == config
    assert loaded.serve_arguments() == [
        "serve", "--repo-root", str(tmp_path.resolve()), "--host", "127.0.0.1",
        "--port", "8080", "--data-refresh-time", "19:00",
    ]
    assert loaded.log_path == tmp_path.resolve() / "state" / "paper_trading" / "logs" / "pte.log"


def test_service_config_rejects_non_local_binding_and_relative_root(tmp_path: Path) -> None:
    from paper_trading_engine.service_config import ServiceConfig

    with pytest.raises(ValueError, match="absolute"):
        ServiceConfig(repo_root=Path("relative"))
    with pytest.raises(ValueError, match="localhost"):
        ServiceConfig(repo_root=tmp_path.resolve(), host="0.0.0.0")
