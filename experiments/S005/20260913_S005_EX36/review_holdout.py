from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

import pandas as pd


def _plain_text(value: object) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", str(value), flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Print deterministic EX36 manual-review packets.")
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--chars", type=int, default=4000)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    data = pd.read_csv(repo / ".tmp/research_cache/S005/news/ex36_sina_sample.csv.gz")
    numbers = data["sample_id"].str.extract(r"(\d+)$", expand=False).astype(int)
    selected = data.loc[numbers.between(args.start, args.end)]
    for row in selected.itertuples(index=False):
        content = _plain_text(row.content)
        print(f"\n===== {row.sample_id} | {row.pub_time} | {row.title} | plain_chars={len(content)} =====")
        print(content[: args.chars])


if __name__ == "__main__":
    main()

