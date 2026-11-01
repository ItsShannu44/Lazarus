import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.orchestrator import Orchestrator
from app.store import Store
from app.textutil import PatchError, apply_edit, log_excerpt, paths_in_text

from .fakes import CI_LOG, FakeGitHub, FakeLLM, failed_run


def make(ci_outcomes=None, fixes=None, **overrides):
    s = Settings()
    s.poll_interval_s, s.verify_timeout_s, s.allowed_repos = 0, 5, []
    s.max_retries, s.min_confidence = 2, 50
    s.auto_heal, s.push_low_confidence = False, False  # the classic behaviour; self-healing has its own tests
    for k, v in overrides.items():
        setattr(s, k, v)
    gh, llm = FakeGitHub(ci_outcomes), FakeLLM(fixes)
    return Orchestrator(s, gh, llm, Store()), gh, llm


def run_pipeline(orch):
    async def go():
        run = await orch.start_manual("me/demo-app", 555)
        await asyncio.gather(*orch._tasks)
        return run
    return asyncio.run(go())


# ---------------------------------------------------------------- happy path
def test_fix_first_try_opens_pr():
    orch, gh, llm = make()
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval", run.escalation_reason
    assert run.error_type == "test_failure"
    # LLM pick + file named in the log; hallucinated path dropped
    assert run.files == ["src/shop/cart.py", "tests/test_cart.py"]
    assert "+    if n >= 0:" in run.diff and "-    if n > 0:" in run.diff
    assert run.confidence == 87 and run.pr_number == 1
    committed = next(iter(gh.commits.values()))["files"]["src/shop/cart.py"][0]
    assert "if n >= 0:" in committed
    pr = gh.prs[1]
    assert pr["base"] == "main" and pr["head"].startswith("lazarus/")
    agents_seen = [s.agent for s in run.steps]
    for a in ["Orchestrator", "Log Analyst", "Code Locator", "Fixer", "Verifier", "PR Agent"]:
        assert a in agents_seen
    assert run.minutes_saved == 45


def test_approve_merges_and_reject_closes():
    orch, gh, _ = make()
    run = run_pipeline(orch)
    asyncio.run(orch.approve(run))
    assert run.status == "merged" and gh.merged == [1]

    orch2, gh2, _ = make()
    run2 = run_pipeline(orch2)
    asyncio.run(orch2.reject(run2, "not convinced"))
    assert run2.status == "rejected" and gh2.prs[1]["state"] == "closed"
    assert run2.minutes_saved == 0


# ---------------------------------------------------------------- retry loop
def test_retry_after_red_ci_then_green():
    bad = {"reasoning": "try", "confidence": 70,
           "edits": [{"path": "src/shop/cart.py", "search": "        return True", "replace": "        return 1 == 1"}]}
    orch, gh, llm = make(ci_outcomes=["failure", "success"], fixes=[bad, FakeLLM().fixes[0]])
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval"
    assert len(run.attempts) == 2 and run.attempts[0].passed is False
    assert len(gh.deleted) == 1  # failed fix branch cleaned up
    second_prompt = [p for a, p in llm.calls if a == "Fixer"][-1]
    assert "PREVIOUS ATTEMPT #1 FAILED CI" in second_prompt


def test_escalates_after_max_retries():
    orch, gh, _ = make(ci_outcomes=["failure"] * 3)
    run = run_pipeline(orch)
    assert run.status == "needs_human"
    assert len(run.attempts) == 3 and "3 attempts" in run.escalation_reason
    assert not gh.prs


def test_low_confidence_does_not_push():
    low = {"reasoning": "unsure", "confidence": 20,
           "edits": [{"path": "src/shop/cart.py", "search": "    if n > 0:", "replace": "    if n >= 0:"}]}
    orch, gh, _ = make(fixes=[low])
    run = run_pipeline(orch)
    assert run.status == "needs_human" and not gh.commits and run.diff


def test_bad_patch_is_retried_without_burning_ci():
    bad = {"reasoning": "x", "confidence": 90,
           "edits": [{"path": "src/shop/cart.py", "search": "if n > 1:", "replace": "y"}]}
    orch, gh, llm = make(fixes=[bad, FakeLLM().fixes[0]])
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval" and len(gh.commits) == 1
    assert "COULD NOT BE APPLIED" in [p for a, p in llm.calls if a == "Fixer"][-1]


def test_refuses_to_edit_workflows():
    evil = {"reasoning": "x", "confidence": 99,
            "edits": [{"path": ".github/workflows/ci.yml", "search": "on: [push]", "replace": "on: []"}]}
    orch, gh, _ = make(fixes=[evil])
    run = run_pipeline(orch)
    assert run.status == "needs_human" and not gh.commits


# ---------------------------------------------------------------- webhook + API
def client_with(orch, secret=""):
    from app import main
    main.settings.webhook_secret = secret
    main.app.state.orch = orch
    return TestClient(main.app)


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_flow_and_guards():
    orch, gh, _ = make()
    with client_with(orch, secret="s3cret") as c:
        body = json.dumps({"action": "completed", "workflow_run": failed_run(777),
                           "repository": {"full_name": "me/demo-app"}}).encode()
        hdr = {"X-GitHub-Event": "workflow_run", "Content-Type": "application/json"}

        assert c.post("/webhook/github", content=body, headers={**hdr, "X-Hub-Signature-256": "sha256=bad"}).status_code == 401
        r = c.post("/webhook/github", content=body, headers={**hdr, "X-Hub-Signature-256": sign("s3cret", body)})
        assert "started" in r.json(), r.json()
        run_id = r.json()["started"]

        # duplicate delivery is ignored
        r2 = c.post("/webhook/github", content=body, headers={**hdr, "X-Hub-Signature-256": sign("s3cret", body)})
        assert "ignored" in r2.json()

        # failures on our own fix branches never start a new incident
        wr = failed_run(778) | {"head_branch": "lazarus/run-9-a1"}
        b3 = json.dumps({"action": "completed", "workflow_run": wr, "repository": {"full_name": "me/demo-app"}}).encode()
        r3 = c.post("/webhook/github", content=b3, headers={**hdr, "X-Hub-Signature-256": sign("s3cret", b3)})
        assert r3.json()["handled"] == "verifier_nudge"

        for _ in range(100):
            if c.get(f"/api/runs/{run_id}").json()["status"] == "awaiting_approval":
                break
            time.sleep(0.05)
        detail = c.get(f"/api/runs/{run_id}").json()
        assert detail["status"] == "awaiting_approval" and detail["steps"]
        assert c.get("/api/runs").json()[0]["id"] == run_id
        assert c.post(f"/api/runs/{run_id}/approve").json()["status"] == "merged"
        assert c.post(f"/api/runs/{run_id}/approve").status_code == 409
        assert c.get("/api/stats").json()["merged"] == 1


# ---------------------------------------------------------------- utilities
def test_log_excerpt_strips_noise_and_keeps_errors():
    ex = log_excerpt(CI_LOG)
    assert "\x1b[" not in ex and "2026-10-31T" not in ex and "##[group]" not in ex
    assert "FAILED tests/test_cart.py::test_check_allows_zero" in ex


def test_paths_in_text_matches_runner_paths():
    assert paths_in_text(CI_LOG, ["src/shop/cart.py", "tests/test_cart.py", "x.py"]) == [
        "tests/test_cart.py", "src/shop/cart.py"]


def test_apply_edit_exact_loose_and_ambiguous():
    src = "a = 1\nif x:   \n    y()\nb = 2\n"
    assert apply_edit(src, "a = 1", "a = 2", "f") == src.replace("a = 1", "a = 2")
    loose = apply_edit(src, "if x:\n    y()", "if z:\n    y()", "f")  # trailing spaces differ
    assert "if z:\n    y()\nb = 2" in loose
    with pytest.raises(PatchError):
        apply_edit("x\nx\n", "x", "y", "f")
    with pytest.raises(PatchError):
        apply_edit(src, "nope", "y", "f")
