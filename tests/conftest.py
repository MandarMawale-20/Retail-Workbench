from __future__ import annotations

from pathlib import Path

import pytest

from app.evaluation.sample_data import generate
from app.ingestion.loader import validate_file
from app.query.catalog import Catalog
from app.schemas import PipelineOptions
from app.services.chat import create_session
from app.services.context import Services
from app.services.pipeline import run_pipeline
from tests.oracle.oracle import Oracle

SIX = ["products", "stores", "orders", "returns", "inventory", "customers"]
OWNER = "tester"


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("data")
    generate(path)
    return path


@pytest.fixture(scope="session")
def oracle(data_dir) -> Oracle:
    return Oracle(data_dir)


def make_files(data_dir: Path, names: list[str]):
    return [validate_file(data_dir / f"{n}.csv", n, max_upload_mb=100) for n in names]


@pytest.fixture(scope="session")
def main_env(data_dir, tmp_path_factory):
    svc = Services.create(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    result = run_pipeline(OWNER, make_files(data_dir, SIX), PipelineOptions(), svc)
    catalog = Catalog.load(Path(result["artifacts"]["run_dir"]))
    return svc, result, catalog


@pytest.fixture
def session(main_env):
    svc, result, _ = main_env
    return svc, create_session(svc, OWNER, result["run_id"])


@pytest.fixture
def rel_ids(main_env):
    _, _, catalog = main_env
    return {(r.left_column_id, r.right_column_id): r.relationship_id for r in catalog.graph.relationships}
