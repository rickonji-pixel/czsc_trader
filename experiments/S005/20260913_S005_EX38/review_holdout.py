from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = json.loads((experiment / "artifacts/protocol.json").read_text(encoding="utf-8"))
    raw = pd.read_csv(repo / protocol["local_cache"], keep_default_na=False)
    subset = raw.iloc[max(args.start - 1, 0): max(args.start - 1, 0) + args.count]
    print(subset[["sample_id", "pub_time", "title", "content"]].to_json(orient="records", force_ascii=True))


if __name__ == "__main__":
    main()
