import csv
import time

from app.ingestion.loader import validate_file
from app.schemas import PipelineOptions
from app.services.context import Services
from app.services.pipeline import run_pipeline


def test_pipeline_handles_100k_rows(tmp_path):
    source = tmp_path / "orders.csv"
    with source.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["order_id", "product_id", "quantity", "unit_price", "order_date"])
        for i in range(100_000):
            writer.writerow([f"O{i}", f"P{i % 100}", 1, "10.00", "2024-01-01"])
    svc = Services.create(artifacts_dir=tmp_path / "artifacts")
    started = time.perf_counter()
    result = run_pipeline("scale", [validate_file(source, "orders", max_upload_mb=100)], PipelineOptions(), svc)
    assert result["status"] in ("completed", "completed_with_warnings")
    assert result["datasets"][0]["rows_raw"] == 100_000
    assert time.perf_counter() - started < 180
