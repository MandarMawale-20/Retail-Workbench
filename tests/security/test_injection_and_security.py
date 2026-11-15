from pathlib import Path

import pytest

from app.config import Settings
from app.errors import AppError
from app.evaluation.injection import run_suite, summarize
from app.ingestion.loader import ensure_within
from app.observability.tracer import redact


@pytest.fixture(scope="module")
def suite(tmp_path_factory):
    return run_suite(tmp_path_factory.mktemp("inj"), Settings(llm_provider="none"))


def test_attack_success_rate_is_zero_and_every_attack_has_blocked_at(suite):
    summary = summarize(suite)
    assert summary["attacks"] >= 12 and summary["asr"] == 0.0 and summary["succeeded"] == 0
    assert summary["unblocked"] == [] and all(r["blocked_at"] != "none" for r in suite)


def test_suite_exercises_every_defence_stage(suite):
    stages = {r["blocked_at"] for r in suite}
    assert {"prompt_fencing", "planner_output_schema", "validator", "tool_allowlist", "claim_verifier"} <= stages
    vectors = {r["vector"] for r in suite}
    assert {"cell_to_tool_result", "cell_to_schema", "question", "malicious_plan", "tool_abuse", "narrator_output"} <= vectors


def test_path_containment(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.csv").write_text("a")
    with pytest.raises(AppError):
        ensure_within(root / ".." / "outside.csv", root)
    link = root / "link"
    link.symlink_to(tmp_path / "outside.csv")
    with pytest.raises(AppError):
        ensure_within(link, root)
    assert ensure_within(root / "ok.csv", root)


def test_secrets_are_redacted_from_traces():
    assert "nvapi-" not in str(redact({"k": "Bearer abcdefghijklmnop and nvapi-ABCDEFGH12345678"}))


def test_no_secret_defaults_and_env_example_has_placeholders_only():
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assert "nvapi-REPLACE_ME" in text and Settings().nvidia_api_key == ""
