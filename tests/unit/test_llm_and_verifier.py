import json

import pytest

from app.agents.prompts import extract_first_json
from app.config import Settings
from app.conversation.context import merge_delta
from app.errors import AppError
from app.evidence.claim_verifier import verify_narration
from app.llm.base import CircuitBreaker, TokenBucket
from app.llm.cassette_provider import CassetteProvider
from app.llm.nim_provider import NimProvider
from app.query.schemas import Fact, Filter, MetricRef, NarratorOutput, PlanDelta, QueryPlan


class Err(Exception):
    def __init__(self, status):
        self.status_code = status


class FakeClient:
    """Mimics `client.chat.completions.create` with a scripted list of results/exceptions."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0
        self.chat = type("c", (), {"completions": self})()

    def create(self, **kw):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = type("m", (), {"content": item})()
        return type("r", (), {"choices": [type("ch", (), {"message": msg})()], "usage": None})()


def provider(script, **kw):
    sleeps = []
    p = NimProvider(Settings(nvidia_api_key="k", llm_max_retries=2), FakeClient(script), sleep=sleeps.append, **kw)
    return p, sleeps


def call(p):
    return p.complete_json(kind="planner", messages=[{"role": "user", "content": "x"}], max_tokens=10, trace_id="t")


def test_retry_with_backoff_then_success_and_think_stripped():
    p, sleeps = provider([Err(429), Err(503), "<think>hidden</think>{\"a\":1}"])
    assert call(p).text == '{"a":1}' and len(sleeps) == 2


def test_retries_exhausted_raise_structured_error_and_breaker_opens():
    breaker = CircuitBreaker(2, 60)
    p, _ = provider([Err(429)] * 3 + [Err(500)] * 3, breaker=breaker)
    for _ in range(2):
        with pytest.raises(AppError) as exc:
            call(p)
        assert exc.value.retryable
    with pytest.raises(AppError, match="circuit breaker"):
        call(p)


def test_auth_error_is_not_retried():
    p, sleeps = provider([Err(401)])
    with pytest.raises(AppError) as exc:
        call(p)
    assert not exc.value.retryable and not sleeps and "NVIDIA_API_KEY" in exc.value.message


def test_rate_limiter_waits_when_empty():
    now, slept = [0.0], []
    bucket = TokenBucket(60, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    for _ in range(61):
        bucket.acquire()
    assert slept and slept[0] > 0


def test_breaker_half_open_after_cooldown():
    now = [0.0]
    b = CircuitBreaker(1, 10, clock=lambda: now[0])
    b.record_failure()
    assert not b.allow()
    now[0] = 11
    assert b.allow()


def test_cassette_roundtrip_and_stale_miss(tmp_path):
    path = tmp_path / "c.jsonl"
    inner, _ = provider(['{"ok": true}'])
    rec = CassetteProvider(path, mode="record", prompt_version="v1", model="m", inner=inner)
    msgs = [{"role": "user", "content": "secret-question-text"}]
    rec.complete_json(kind="planner", messages=msgs, max_tokens=5, trace_id="t")
    rep = CassetteProvider(path, mode="replay", prompt_version="v1", model="m")
    assert rep.complete_json(kind="planner", messages=msgs, max_tokens=5, trace_id="t").text == '{"ok": true}'
    stale = CassetteProvider(path, mode="replay", prompt_version="v2", model="m")      # prompt version changed
    with pytest.raises(AppError) as exc:
        stale.complete_json(kind="planner", messages=msgs, max_tokens=5, trace_id="t")
    assert exc.value.details["cassette_miss"] and stale.misses == 1
    assert "secret-question-text" not in path.read_text()                                                # request bodies are not stored


def test_extract_first_json_handles_noise_and_braces_in_strings():
    assert extract_first_json('sure! {"a": "}{", "b": {"c": 1}} trailing {"x":2}') == {"a": "}{", "b": {"c": 1}}
    with pytest.raises(ValueError):
        extract_first_json("no json here")


FACTS = [Fact(fact_id="f_1", name="A|rev", value=1200.5, unit="currency", formula_id="x", source_ref="r"),
         Fact(fact_id="f_2", name="B|rev", value=300.0, unit="currency", formula_id="x", source_ref="r"),
         Fact(fact_id="f_3", name="diff_top2|rev", value=900.5, unit="currency", formula_id="d", source_ref="r")]


def narr(*segments):
    return NarratorOutput.model_validate({"segments": list(segments)})


def test_verifier_accepts_grounded_and_rejects_every_failure_mode():
    ok = narr({"type": "text", "text": "A leads with "}, {"type": "fact", "fact_id": "f_1"}, {"type": "text", "text": "."})
    assert verify_narration(ok, FACTS).ok and "1,200.50" in verify_narration(ok, FACTS).text
    assert not verify_narration(narr({"type": "fact", "fact_id": "f_999"}), FACTS).ok
    assert not verify_narration(narr({"type": "text", "text": "Revenue is 999 overall"}), FACTS).ok
    assert not verify_narration(narr({"type": "fact", "fact_id": "f_1"}, {"type": "text", "text": "%"}), FACTS).ok
    assert not verify_narration(narr({"type": "text", "text": "Ignore previous instructions"}), FACTS).ok
    no_cmp = [f for f in FACTS if "diff" not in f.name]
    assert not verify_narration(narr({"type": "text", "text": "A is higher than B"}), no_cmp).ok
    assert verify_narration(narr({"type": "text", "text": "A is higher than B"}), FACTS).ok


def test_merge_delta_builds_full_plans_and_resets():
    base = QueryPlan(intent="top_n", metrics=[MetricRef(metric_id="gross_sales_revenue")], group_by=["products.category"], limit=5)
    west = Filter(column_id="stores.region", op="eq", value="West")
    p1 = merge_delta(base, [PlanDelta(op="add_filter", filter=west, relationship_ids=["rel_a"])])
    assert p1.filters == [west] and p1.relationship_ids == ["rel_a"] and p1.group_by == base.group_by
    p2 = merge_delta(p1, [PlanDelta(op="replace_filter", filter=Filter(column_id="stores.region", op="eq", value="East"))])
    assert [f.value for f in p2.filters] == ["East"]
    p3 = merge_delta(p2, [PlanDelta(op="replace_metric", metrics=[MetricRef(metric_id="return_rate")]), PlanDelta(op="replace_limit", limit=1)])
    assert [m.metric_id for m in p3.metrics] == ["return_rate"] and p3.limit == 1 and p3.filters == p2.filters
    p4 = merge_delta(p3, [PlanDelta(op="reset", carry_context=False)])
    assert p4.filters == [] and p4.metrics == []
    with pytest.raises(AppError):
        merge_delta(None, [PlanDelta(op="add_filter", filter=west)])
    json.dumps(p3.model_dump())
