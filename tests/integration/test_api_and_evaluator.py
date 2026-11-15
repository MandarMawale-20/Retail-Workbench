import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.evaluation.compare import strip_volatile
from app.main import create_app
from app.services.context import Services

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Services.create(artifacts_dir=tmp_path)))


def upload(client, data_dir, names, owner="alice", **form):
    files = [("files", (f"{n}.csv", (data_dir / f"{n}.csv").read_bytes(), "text/csv")) for n in names]
    return client.post("/api/runs", files=files, data=form, headers={"X-Owner-Id": owner})


def test_api_flow_and_owner_isolation(client, data_dir):
    r = upload(client, data_dir, ["products", "stores", "orders"])
    assert r.status_code == 200 and r.json()["status"] in ("completed", "completed_with_warnings")
    run_id = r.json()["run_id"]
    sid = client.post("/api/sessions", json={"run_id": run_id}, headers={"X-Owner-Id": "alice"}).json()["session_id"]
    chat = client.post(f"/api/sessions/{sid}/chat", json={"question": "Top 3 categories by revenue"}, headers={"X-Owner-Id": "alice"}).json()
    assert chat["status"] == "ok" and chat["evidence"]["rows_considered"] > 0
    for path in (f"/api/runs/{run_id}", f"/api/runs/{run_id}/issues", f"/api/traces/{chat['trace_id']}",
                 f"/api/evidence/{chat['evidence']['evidence_id']}", f"/api/sessions/{sid}/turns"):
        assert client.get(path, headers={"X-Owner-Id": "alice"}).status_code == 200
        assert client.get(path, headers={"X-Owner-Id": "bob"}).status_code == 404                # server-owned scope
    assert client.post("/api/sessions", json={"run_id": run_id}, headers={"X-Owner-Id": "bob"}).status_code == 404
    assert client.get("/").status_code == 200


def test_api_rejects_bad_uploads(client):
    assert client.post("/api/runs", files=[("files", ("x.txt", b"a,b\n", "text/plain"))]).status_code == 400
    assert client.post("/api/runs", files=[("files", ("e.csv", b"", "text/csv"))]).json()["error"]["code"] == "INVALID_FILE"


def test_async_run_and_job_status(client, data_dir):
    import time
    r = upload(client, data_dir, ["stores"], run_async="true")
    assert r.status_code == 202
    for _ in range(100):
        job = client.get(f"/api/jobs/{r.json()['job_id']}", headers={"X-Owner-Id": "alice"}).json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed"
    assert client.get(f"/api/jobs/{r.json()['job_id']}", headers={"X-Owner-Id": "bob"}).status_code == 404


def run_eval(tmp_path, name, *extra):
    out = tmp_path / name
    env = dict(os.environ)
    env.update({"NVIDIA_API_KEY": "", "HOME": str(tmp_path)})
    proc = subprocess.run([sys.executable, "-m", "app.evaluate", "--input", "evaluation_cases/cases.json", "--output", str(out / "r.json"),
                           "--artifacts-dir", str(out / "art"), *extra], cwd=ROOT, capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads((out / "r.json").read_text())


def test_evaluator_replay_is_deterministic_keyless_and_isolated(tmp_path):
    a = run_eval(tmp_path, "a", "--provider", "replay", "--cassettes", "evaluation_cases/cassettes", "--skip-injection")
    b = run_eval(tmp_path, "b", "--provider", "replay", "--cassettes", "evaluation_cases/cassettes", "--skip-injection")
    assert strip_volatile(a) == strip_volatile(b)
    cases = {c["case_id"]: c for c in a["cases"]}
    required = {"dataset", "profile_before", "issues", "cleaning_plan", "profile_after", "validation", "chat_evaluation", "artifacts"}
    assert all(required <= set(c) for c in a["cases"])
    assert cases["bad_case_missing_file"]["status"] == "error" and cases["bad_case_malformed"]["errors"][0]["code"] == "CASE_INPUT_INVALID"
    assert cases["bad_case_malformed"]["extra"] == {"custom_field": "preserved"}                 # unknown fields preserved
    healthy = [c for c in a["cases"] if not c["case_id"].startswith("bad_case")]
    assert all(c["status"] == "ok" for c in healthy), [(c["case_id"], c["status"]) for c in healthy]
    assert a["summary"]["cassette"]["misses"] == 0 and a["summary"]["cassette"]["hits"] > 0
    replay = a["summary"]["by_path"]["replay"]
    assert replay["exact_answer_accuracy"] == 1.0 and replay["grounding_rate"] == 1.0
    turn = cases["retail_chat"]["chat_evaluation"][0]
    assert turn["tool_log"] and turn["trace"]["events"] and turn["evidence"]["facts"]


def test_evaluator_fallback_path_and_cli_errors(tmp_path):
    fb = run_eval(tmp_path, "fb", "--provider", "fallback", "--skip-injection")
    assert set(fb["summary"]["by_path"]) >= {"fallback"} and fb["summary"]["by_status"].get("ok", 0) >= 10
    proc = subprocess.run([sys.executable, "-m", "app.evaluate", "--input", str(tmp_path / "missing.json"), "--output", str(tmp_path / "o.json"),
                           "--artifacts-dir", str(tmp_path / "x")], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 2 and "fatal" in proc.stderr
