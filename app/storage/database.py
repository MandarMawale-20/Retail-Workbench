"""SQLite metadata store (WAL) with hand-written, owner-scoped repositories."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
  status TEXT NOT NULL, policy TEXT, created_at TEXT, finished_at TEXT, error_json TEXT, warnings_json TEXT,
  UNIQUE(owner_id, fingerprint));
CREATE TABLE IF NOT EXISTS dataset_versions(version_id TEXT, run_id TEXT, owner_id TEXT, name TEXT, stage TEXT,
  artifact_path TEXT, sha256 TEXT, row_count INT, parent_version_id TEXT, PRIMARY KEY(version_id, run_id, stage));
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, run_id TEXT, owner_id TEXT, kind TEXT, path TEXT, sha256 TEXT, bytes INT);
CREATE TABLE IF NOT EXISTS profiles(profile_id TEXT PRIMARY KEY, run_id TEXT, owner_id TEXT, version_id TEXT, profile_json TEXT, profile_hash TEXT);
CREATE TABLE IF NOT EXISTS issues(issue_id TEXT, run_id TEXT, owner_id TEXT, issue_json TEXT, PRIMARY KEY(issue_id, run_id));
CREATE TABLE IF NOT EXISTS role_bindings(run_id TEXT, owner_id TEXT, version_id TEXT, role TEXT, column_id TEXT, confidence REAL, status TEXT);
CREATE TABLE IF NOT EXISTS cleaning_steps(step_id TEXT, run_id TEXT, owner_id TEXT, step_json TEXT, status TEXT, PRIMARY KEY(step_id, run_id));
CREATE TABLE IF NOT EXISTS relationship_graphs(graph_version TEXT PRIMARY KEY, run_id TEXT, owner_id TEXT, graph_json TEXT);
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, owner_id TEXT, run_id TEXT, state_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS chat_turns(turn_id TEXT PRIMARY KEY, session_id TEXT, owner_id TEXT, idem_key TEXT, question TEXT,
  status TEXT, plan_json TEXT, validation_json TEXT, execution_json TEXT, evidence_id TEXT, answer_json TEXT, trace_id TEXT,
  UNIQUE(session_id, idem_key));
CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT, owner_id TEXT, evidence_json TEXT, PRIMARY KEY(evidence_id, owner_id));
CREATE TABLE IF NOT EXISTS traces(trace_id TEXT, owner_id TEXT, seq INT, ts TEXT, component TEXT, status TEXT, duration_ms INT,
  event_json TEXT, PRIMARY KEY(trace_id, seq));
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, run_id TEXT, owner_id TEXT, status TEXT, attempts INT DEFAULT 0,
  retryable INT, error_json TEXT, created_at TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS advisor_cache(cache_key TEXT PRIMARY KEY, response_json TEXT);
CREATE TABLE IF NOT EXISTS llm_cache(cache_key TEXT PRIMARY KEY, response_text TEXT, model TEXT, created_at TEXT);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.executescript(DDL)
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        con = self._connect()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
