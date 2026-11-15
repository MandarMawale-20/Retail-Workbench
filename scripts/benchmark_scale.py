"""Measure end-to-end pipeline throughput for configurable CSV inputs."""
from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion.loader import validate_file
from app.schemas import PipelineOptions
from app.services.context import Services
from app.services.pipeline import run_pipeline


def benchmark(rows: int) -> tuple[int, float]:
    with tempfile.TemporaryDirectory(prefix=f"retail-scale-{rows}-") as root:
        work = Path(root)
        source = work / "orders.csv"
        with source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["order_id", "product_id", "quantity", "unit_price", "order_date"])
            for index in range(rows):
                writer.writerow([f"O{index}", f"P{index % 100}", 1, "10.00", "2024-01-01"])
        started = time.perf_counter()
        result = run_pipeline(
            "scale-benchmark",
            [validate_file(source, "orders", max_upload_mb=500)],
            PipelineOptions(),
            Services.create(artifacts_dir=work / "artifacts"),
        )
        elapsed = time.perf_counter() - started
        actual = result["datasets"][0]["rows_raw"]
        if result["status"] not in ("completed", "completed_with_warnings") or actual != rows:
            raise RuntimeError(f"benchmark failed: status={result['status']}, rows={actual}")
        return actual, elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", nargs="+", type=int, default=[100_000, 500])
    args = parser.parse_args()
    for rows in args.rows:
        actual, elapsed = benchmark(rows)
        print(f"rows={actual} elapsed_seconds={elapsed:.2f}")


if __name__ == "__main__":
    main()
