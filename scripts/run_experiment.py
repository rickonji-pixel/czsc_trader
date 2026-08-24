"""Run the frozen pre-2026 588080 champion-challenger experiment."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from czsc_trader.experiments import run_pre2026_experiment
from czsc_trader.research import create_output_dir


if __name__ == "__main__":
    protocol_path = Path("configs/experiments/588080_reentry_v1.json")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise SystemExit("experiment protocol must stop before 2026")
    output_dir = create_output_dir(
        Path("outputs"),
        "588080.SH",
        datetime.now(ZoneInfo("Asia/Shanghai")).date(),
    )
    summary = run_pre2026_experiment(
        Path("data/raw"), Path("configs/rule_baselines"), output_dir
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
