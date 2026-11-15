"""Owner-scoped repositories. Every read/write takes `owner_id`; foreign ownership raises ISOLATION_VIOLATION."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.common import canonical_json
from app.errors import AppError
from app.storage.database import Database


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dump(obj: Any) -> str:
    return canonical_json(obj)


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ runs
    def find_run_by_fingerprint(self, owner_id: str, fingerprint: str) -> dict | None:
        rows = self.db.query("SELECT * FROM runs WHERE owner_id=? AND fingerprint=?", (owner_id, fingerprint))
        return dict(rows[0]) if rows else None

    def save_run(self, owner_id: str, run_id: str, fingerprint: str, status: str, policy: str,
                 error: dict | None = None, warnings: list[str] | None = None) -> None:
        with self.db.tx() as con:
            con.execute(
                "INSERT INTO runs(run_id, owner_id, fingerprint, status, policy, created_at, finished_at, error_json, warnings_json) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                "finished_at=excluded.finished_at, error_json=excluded.error_json, warnings_json=excluded.warnings_json",
                (run_id, owner_id, fingerprint, status, policy, _now(), _now(), _dump(error), _dump(warnings or [])),
            )

    def set_run_status(self, owner_id: str, run_id: str, status: str) -> None:
        with self.db.tx() as con:
            con.execute("UPDATE runs SET status=? WHERE run_id=? AND owner_id=?", (status, run_id, owner_id))

    def delete_run_children(self, owner_id: str, run_id: str) -> None:
        with self.db.tx() as con:
            for table in ("dataset_versions", "artifacts", "profiles", "issues", "role_bindings", "cleaning_steps",
                          "relationship_graphs"):
                con.execute(f"DELETE FROM {table} WHERE run_id=? AND owner_id=?", (run_id, owner_id))  # noqa: S608

    def get_run(self, owner_id: str, run_id: str) -> dict:
        rows = self.db.query("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if not rows or rows[0]["owner_id"] != owner_id:
            raise AppError("ISOLATION_VIOLATION", "run not found for this owner", stage="auth")
        return dict(rows[0])

    # ------------------------------------------------------------ run documents
    def save_documents(self, owner_id: str, run_id: str, *, versions: list[dict], artifacts: list[dict],
                       profiles: list[dict], issues: list[dict], bindings: list[dict], steps: list[dict],
                       graph: dict | None) -> None:
        with self.db.tx() as con:
            for v in versions:
                con.execute("INSERT OR REPLACE INTO dataset_versions VALUES(?,?,?,?,?,?,?,?,?)",
                            (v["version_id"], run_id, owner_id, v["name"], v["stage"], v["artifact_path"], v["sha256"],
                             v["row_count"], v.get("parent_version_id")))
            for a in artifacts:
                con.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?)",
                            (f"{run_id}:{a['kind']}", run_id, owner_id, a["kind"], a["path"], a["sha256"], a["bytes"]))
            for p in profiles:
                con.execute("INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?)",
                            (f"{p['dataset_version_id']}:{p['stage']}:{run_id}", run_id, owner_id, p["dataset_version_id"],
                             _dump(p), p["profile_hash"]))
            for i in issues:
                con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)", (i["issue_id"], run_id, owner_id, _dump(i)))
            for b in bindings:
                con.execute("INSERT INTO role_bindings VALUES(?,?,?,?,?,?,?)",
                            (run_id, owner_id, b["dataset_version_id"], b["role"], b["column_id"], b["confidence"], b["status"]))
            for s in steps:
                con.execute("INSERT OR REPLACE INTO cleaning_steps VALUES(?,?,?,?,?)",
                            (s["step_id"], run_id, owner_id, _dump(s), s["status"]))
            if graph:
                con.execute("INSERT OR REPLACE INTO relationship_graphs VALUES(?,?,?,?)",
                            (graph["graph_version"], run_id, owner_id, _dump(graph)))

    def run_documents(self, owner_id: str, run_id: str, table: str, column: str) -> list[dict]:
        self.get_run(owner_id, run_id)
        rows = self.db.query(f"SELECT {column} FROM {table} WHERE run_id=? AND owner_id=?", (run_id, owner_id))  # noqa: S608
        return [json.loads(r[0]) for r in rows]

    def list_artifacts(self, owner_id: str, run_id: str) -> list[dict]:
        self.get_run(owner_id, run_id)
        return [dict(r) for r in self.db.query("SELECT kind, path, sha256, bytes FROM artifacts WHERE run_id=? AND owner_id=?",
                                               (run_id, owner_id))]

    # ------------------------------------------------------------ sessions and turns
    def create_session(self, owner_id: str, session_id: str, run_id: str, state: dict) -> None:
        self.get_run(owner_id, run_id)
        with self.db.tx() as con:
            con.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (session_id, owner_id, run_id, _dump(state), _now()))

    def get_session(self, owner_id: str, session_id: str) -> dict:
        rows = self.db.query("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if not rows or rows[0]["owner_id"] != owner_id:
            raise AppError("ISOLATION_VIOLATION", "session not found for this owner", stage="auth")
        row = dict(rows[0])
        row["state"] = json.loads(row.pop("state_json"))
        return row

    def update_session_state(self, owner_id: str, session_id: str, state: dict) -> None:
        with self.db.tx() as con:
            con.execute("UPDATE sessions SET state_json=? WHERE session_id=? AND owner_id=?",
                        (_dump(state), session_id, owner_id))

    def reset_session(self, owner_id: str, session_id: str, state: dict) -> None:
        self.get_session(owner_id, session_id)
        with self.db.tx() as con:
            con.execute("UPDATE sessions SET state_json=? WHERE session_id=? AND owner_id=?",
                        (_dump(state), session_id, owner_id))
            con.execute("DELETE FROM chat_turns WHERE session_id=? AND owner_id=?", (session_id, owner_id))

    def find_turn_by_idem(self, owner_id: str, session_id: str, idem_key: str) -> dict | None:
        rows = self.db.query("SELECT * FROM chat_turns WHERE session_id=? AND owner_id=? AND idem_key=?",
                             (session_id, owner_id, idem_key))
        return json.loads(rows[0]["answer_json"]) if rows else None

    def add_turn(self, owner_id: str, turn: dict, idem_key: str | None) -> None:
        with self.db.tx() as con:
            con.execute(
                "INSERT INTO chat_turns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (turn["turn_id"], turn["session_id"], owner_id, idem_key, turn["question"], turn["status"],
                 _dump(turn.get("plan")), _dump(turn.get("validation")), _dump(turn.get("execution")),
                 turn.get("evidence_id"), _dump(turn["answer"]), turn["trace_id"]),
            )

    def list_turns(self, owner_id: str, session_id: str) -> list[dict]:
        self.get_session(owner_id, session_id)
        rows = self.db.query("SELECT * FROM chat_turns WHERE session_id=? AND owner_id=? ORDER BY rowid", (session_id, owner_id))
        return [{"turn_id": r["turn_id"], "question": r["question"], "status": r["status"],
                 "plan": json.loads(r["plan_json"]), "validation": json.loads(r["validation_json"]),
                 "execution": json.loads(r["execution_json"]), "evidence_id": r["evidence_id"],
                 "answer": json.loads(r["answer_json"]), "trace_id": r["trace_id"]} for r in rows]

    def save_evidence(self, owner_id: str, evidence_id: str, evidence: dict) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?)", (evidence_id, owner_id, _dump(evidence)))

    def get_evidence(self, owner_id: str, evidence_id: str) -> dict:
        rows = self.db.query("SELECT evidence_json FROM evidence WHERE evidence_id=? AND owner_id=?", (evidence_id, owner_id))
        if not rows:
            raise AppError("ISOLATION_VIOLATION", "evidence not found for this owner", stage="auth")
        return json.loads(rows[0][0])

    # ------------------------------------------------------------ traces
    def save_trace(self, owner_id: str, trace_id: str, events: list[dict]) -> None:
        with self.db.tx() as con:
            for seq, e in enumerate(events):
                con.execute("INSERT OR REPLACE INTO traces VALUES(?,?,?,?,?,?,?,?)",
                            (trace_id, owner_id, seq, e.get("ts", ""), e["component"], e["status"],
                             e.get("duration_ms", 0), _dump(e)))

    def get_trace(self, owner_id: str, trace_id: str) -> list[dict]:
        rows = self.db.query("SELECT owner_id, event_json FROM traces WHERE trace_id=? ORDER BY seq", (trace_id,))
        if not rows or any(r["owner_id"] != owner_id for r in rows):
            raise AppError("ISOLATION_VIOLATION", "trace not found for this owner", stage="auth")
        return [json.loads(r["event_json"]) for r in rows]

    # ------------------------------------------------------------ caches and jobs
    def llm_cache_get(self, key: str) -> dict | None:
        rows = self.db.query("SELECT response_text, model FROM llm_cache WHERE cache_key=?", (key,))
        return dict(rows[0]) if rows else None

    def llm_cache_put(self, key: str, text: str, model: str) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO llm_cache VALUES(?,?,?,?)", (key, text, model, _now()))

    def advisor_cache_get(self, key: str) -> dict | None:
        rows = self.db.query("SELECT response_json FROM advisor_cache WHERE cache_key=?", (key,))
        return json.loads(rows[0][0]) if rows else None

    def advisor_cache_put(self, key: str, response: dict) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO advisor_cache VALUES(?,?)", (key, _dump(response)))

    def create_job(self, owner_id: str, job_id: str, run_id: str) -> None:
        with self.db.tx() as con:
            con.execute("INSERT INTO jobs(job_id, run_id, owner_id, status, created_at) VALUES(?,?,?,?,?)",
                        (job_id, run_id, owner_id, "queued", _now()))

    def update_job(self, job_id: str, status: str, error: dict | None = None, retryable: bool = False) -> None:
        with self.db.tx() as con:
            con.execute("UPDATE jobs SET status=?, error_json=?, retryable=?, attempts=attempts+?, finished_at=? WHERE job_id=?",
                        (status, _dump(error), int(retryable), 1 if status == "running" else 0, _now(), job_id))

    def get_job(self, owner_id: str, job_id: str) -> dict:
        rows = self.db.query("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not rows or rows[0]["owner_id"] != owner_id:
            raise AppError("ISOLATION_VIOLATION", "job not found for this owner", stage="auth")
        return dict(rows[0])
