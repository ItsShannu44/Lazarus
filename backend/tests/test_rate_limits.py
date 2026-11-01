"""Free-tier rate limits (Groq TPM, Gemini quota) must not stop a fix."""
import asyncio
import json

import httpx

import app.llm as llm_mod
from app.config import Settings
from app.llm import LLM, LLMError, retry_after

GROQ_429 = ('{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` '
            'service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7588, Requested 3405. '
            'Please try again in 7.48s. Need more tokens?","type":"tokens","code":"rate_limit_exceeded"}}')
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def make_llm(handler, gemini=False, patience=150):
    s = Settings()
    s.gemini_api_key, s.fallback_api_key = ("g" if gemini else ""), "k"
    s.fallback_model, s.llm_patience_s = "openai/gpt-oss-120b", patience
    llm = LLM(s)
    llm._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def no_sleep(waits):
    async def fake(sec):
        waits.append(round(sec, 1))
    return fake


def test_retry_after_parses_provider_formats():
    assert retry_after(httpx.Response(429, text=GROQ_429)) == 7.98
    assert retry_after(httpx.Response(429, text="Please try again in 1m2.5s")) == 63.0
    assert retry_after(httpx.Response(429, text="try again in 450ms")) == 0.95
    assert retry_after(httpx.Response(429, headers={"retry-after": "12"})) == 12.0
    assert retry_after(httpx.Response(429, text='{"retryDelay": "33s"}')) == 33.5


def test_switches_to_another_model_when_one_is_rate_limited(monkeypatch):
    waits, used = [], []
    monkeypatch.setattr(llm_mod.asyncio, "sleep", no_sleep(waits))

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}, {"id": "whisper-large-v3"},
                                                      {"id": "meta-llama/llama-4-scout-17b-16e-instruct"}]})
        model = json.loads(req.content)["model"]
        used.append(model)
        if model == "openai/gpt-oss-120b":
            return httpx.Response(429, text=GROQ_429)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = make_llm(handler)
    assert asyncio.run(llm.json("s", "p", SCHEMA, "Fixer")) == {"ok": True}
    assert used == ["openai/gpt-oss-120b", "meta-llama/llama-4-scout-17b-16e-instruct"]
    assert waits == []  # no waiting needed: another model had capacity


def test_waits_exactly_as_long_as_asked_when_no_other_model(monkeypatch):
    waits, calls = [], []
    monkeypatch.setattr(llm_mod.asyncio, "sleep", no_sleep(waits))

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, text=GROQ_429)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = make_llm(handler)
    assert asyncio.run(llm.json("s", "p", SCHEMA, "Fixer")) == {"ok": True}
    assert waits == [8.0, 8.0]  # Groq said 7.48s (+0.5 margin), twice


def test_gemini_quota_hands_over_to_fallback_immediately(monkeypatch):
    waits, hits = [], []
    monkeypatch.setattr(llm_mod.asyncio, "sleep", no_sleep(waits))

    def handler(req):
        hits.append(req.url.host)
        if "generativelanguage" in req.url.host:
            return httpx.Response(429, text='{"error":{"details":[{"retryDelay":"40s"}]}}')
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = make_llm(handler, gemini=True)
    assert asyncio.run(llm.json("s", "p", SCHEMA)) == {"ok": True}
    assert waits == [] and hits[-1] == "api.groq.com"


def test_gives_up_with_a_clear_message_after_patience(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: t[0])

    async def advance(sec):
        t[0] += sec
    monkeypatch.setattr(llm_mod.asyncio, "sleep", advance)

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        return httpx.Response(429, text=GROQ_429)

    llm = make_llm(handler, patience=30)
    try:
        asyncio.run(llm.json("s", "p", SCHEMA))
        raise AssertionError("should have given up")
    except LLMError as e:
        assert "rate limit" in str(e) and "Wait a minute" in str(e)
    assert t[0] <= 31


def test_runner_noise_is_stripped_from_logs():
    from app.textutil import log_excerpt
    raw = "\n".join([
        "[command]/usr/bin/git init /home/runner/work/demo-app/demo-app",
        "hint: Using 'master' as the name for the initial branch.",
        "Temporarily overriding HOME='/home/runner/work/_temp/x' before making global git config changes",
        "  pythonLocation: /opt/hostedtoolcache/Python/3.11.17/x64",
        "pip install -r requirements.txt",
        "ERROR: Could not find a version that satisfies the requirement requests==2.99.0 (from versions: "
        + ", ".join(f"2.{i}.0" for i in range(300)) + ")",
        "ERROR: No matching distribution found for requests==2.99.0",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup.",
        "[command]/usr/bin/git version",
        "Cleaning up orphan processes",
    ])
    ex = log_excerpt(raw)
    assert "No matching distribution found for requests==2.99.0" in ex
    assert "[command]" not in ex and "hint:" not in ex and "pythonLocation" not in ex and "orphan" not in ex
    assert "[trimmed]" in ex and len(ex) < 800


def test_failing_gemini_is_skipped_for_a_while(monkeypatch):
    waits, hits = [], []
    monkeypatch.setattr(llm_mod.asyncio, "sleep", no_sleep(waits))

    def handler(req):
        hits.append("gemini" if "generativelanguage" in req.url.host else "groq")
        if "generativelanguage" in req.url.host:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = make_llm(handler, gemini=True)
    for _ in range(3):  # Log Analyst, Code Locator, Fixer
        assert asyncio.run(llm.json("s", "p", SCHEMA)) == {"ok": True}
    assert hits == ["gemini", "groq", "groq", "groq"]  # Gemini tried once, then skipped
    assert waits == []  # no retry delays paid


def test_gpt_oss_asked_to_think_briefly(monkeypatch):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = make_llm(handler)
    asyncio.run(llm.json("s", "p", SCHEMA))
    assert bodies[0]["reasoning_effort"] == "low" and bodies[0]["max_tokens"] == 2048
