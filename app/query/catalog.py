"""Catalog: the pinned, read-only description of a run (columns, types, roles, graph) used by chat."""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.metrics.binder import RoleIndex
from app.relationships.graph import RelationshipGraph
from app.roles.role_catalog import ID_ROLES
from app.roles.role_inference import is_id_like
from app.schemas import RoleBinding

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class ColumnInfo(BaseModel):
    column_id: str
    dataset: str
    name: str
    inferred_type: str
    sql_type: str = "VARCHAR"
    role: str | None = None
    role_status: str | None = None
    role_confidence: float | None = None
    null_pct: float = 0.0
    distinct_count: int = 0
    is_id: bool = False
    date_ambiguous: bool = False
    top_values: list[tuple[str, int]] = Field(default_factory=list)

    @property
    def is_numeric(self) -> bool:
        return self.sql_type in ("BIGINT", "DOUBLE")

    @property
    def is_date(self) -> bool:
        return self.sql_type in ("DATE", "TIMESTAMP")

    @property
    def is_text(self) -> bool:
        return self.sql_type == "VARCHAR"


class DatasetInfo(BaseModel):
    name: str
    version_id: str
    rows_raw: int
    rows_clean: int
    columns: dict[str, ColumnInfo]
    raw_columns: list[str] = Field(default_factory=list)


class CatalogDoc(BaseModel):
    run_id: str
    owner_id: str
    datasets: dict[str, DatasetInfo]
    bindings: list[RoleBinding]
    graph: RelationshipGraph
    db_file: str = "run.duckdb"


def sql_type_for(inferred: str, values: list[str], name: str, role: str | None) -> str:
    """Physical type for the typed clean table; ids and ambiguous/mixed columns stay VARCHAR."""
    if is_id_like(name, role) or role in ID_ROLES:
        return "VARCHAR"
    if inferred == "integer":
        return "BIGINT"
    if inferred == "decimal":
        return "DOUBLE"
    if inferred == "date" and values:
        if all(_ISO_DATE.match(v) for v in values):
            return "DATE"
        if all(_ISO_TS.match(v) for v in values):
            return "TIMESTAMP"
    return "VARCHAR"


class Catalog:
    def __init__(self, doc: CatalogDoc, run_dir: Path) -> None:
        self.doc, self.run_dir = doc, Path(run_dir)
        self.graph = doc.graph
        self.bindings = doc.bindings
        self.role_index = RoleIndex(doc.bindings, {n: d.rows_clean for n, d in doc.datasets.items()})

    @classmethod
    def load(cls, run_dir: Path) -> Catalog:
        doc = CatalogDoc.model_validate_json((Path(run_dir) / "catalog.json").read_text(encoding="utf-8"))
        return cls(doc, run_dir)

    @property
    def db_path(self) -> Path:
        return self.run_dir / self.doc.db_file

    @property
    def datasets(self) -> dict[str, DatasetInfo]:
        return self.doc.datasets

    @property
    def run_id(self) -> str:
        return self.doc.run_id

    def version_ids(self) -> dict[str, str]:
        return {n: d.version_id for n, d in self.datasets.items()}

    def column(self, column_id: str) -> ColumnInfo | None:
        ds, _, col = column_id.partition(".")
        info = self.datasets.get(ds)
        return info.columns.get(col) if info else None

    def table(self, dataset: str, stage: str = "clean") -> str:
        return quote_ident(f"{stage}_{dataset}")

    def physical(self, column_id: str) -> str:
        return quote_ident(column_id.partition(".")[2])

    def columns_with_role(self, role: str) -> list[ColumnInfo]:
        return [c for d in self.datasets.values() for c in d.columns.values()
                if c.role == role and c.role_status in ("verified", "candidate")]

    def all_columns(self) -> list[ColumnInfo]:
        return [c for d in self.datasets.values() for c in d.columns.values()]
