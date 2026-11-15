"""CLI wrapper: generate seeded sample CSVs with planted data-quality traps."""
from __future__ import annotations

import argparse
from pathlib import Path

from app.evaluation.sample_data import generate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sample_data")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    files = generate(Path(args.out), args.seed)
    print(f"wrote {len(files)} files to {args.out}")
