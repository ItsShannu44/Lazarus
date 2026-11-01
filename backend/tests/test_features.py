"""Features promised on the slides: tests X/Y, escalation issue, watch mode, PR sync, demo controls."""
import asyncio
import time

from app.textutil import count_tests

from .fakes import failed_run
from .test_pipeline import client_with, make, run_pipeline


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def test_test_counts_parsers():
    assert count_tests("===== 2 failed, 4 passed in 0.03s =====") == {"passed": 4, "failed": 2, "total": 6}
    assert count_tests("6 passed in 0.02s") == {"passed": 6, "failed": 0, "total": 6}
    assert count_tests("Tests:       1 failed, 11 passed, 12 total") == {"passed": 11, "failed": 1, "total": 12}
    assert count_tests("  12 passing (40ms)\n  1 failing") == {"passed": 12, "failed": 1, "total": 13}
    assert count_tests("pip install failed: No matching distribution") == {}


def test_green_fix_reports_test_counts_and_pr_mentions_them():
    orch, gh, _ = make()
    run = run_pipeline(orch)
    assert run.tests_after == {"passed": 6, "failed": 0, "total": 6}
    assert "6/6 tests pass" in [s.message for s in run.steps if s.agent == "Verifier"][-1]
    assert "6/6 passing" in gh.prs[1]["body"]


def test_escalation_opens_issue_with_logs_and_notes():
    orch, gh, _ = make(ci_outcomes=["failure"] * 3)
    orch.s.escalate_issue = True
    run = run_pipeline(orch)
    assert run.status == "needs_human" and run.issue_url.endswith("/issues/1")
    body = gh.issues[0]["body"]
    assert "Why I stopped" in body and "CI log excerpt" in body and "test_check_allows_zero" in body
    assert "Attempts" in body and "```diff" in body


def test_escalation_survives_missing_issue_permission():
    orch, gh, _ = make(ci_outcomes=["failure"] * 3)
    orch.s.escalate_issue = True

    async def denied(*a, **k):
        raise Exception("GitHub API 403: Resource not accessible")
    gh.create_issue = denied
    run = run_pipeline(orch)
    assert run.status == "needs_human"
    assert "Issues: Read and write" in run.steps[-1].message


def test_watch_mode_picks_up_only_new_failures():
    orch, gh, _ = make()
    orch.s.allowed_repos = ["me/demo-app"]
    now = orch.started_at
    gh.recent = [
        failed_run(1) | {"updated_at": iso(now - 3600)},                                    # before startup
        failed_run(2) | {"updated_at": iso(now + 5), "head_branch": "lazarus/run-1-a1"},  # our own branch
        failed_run(3) | {"updated_at": iso(now + 5), "conclusion": "success"},
        failed_run(4) | {"updated_at": iso(now + 5)},                                       # the real one
    ]

    async def go():
        started = await orch.poll_once()
        again = await orch.poll_once()  # same run is not started twice
        await asyncio.gather(*orch._tasks)
        return started, again
    started, again = asyncio.run(go())
    assert [r.ci_run_id for r in started] == [4] and again == []
    assert started[0].trigger == "watch" and started[0].status == "awaiting_approval"


def test_pr_merged_on_github_is_synced():
    orch, gh, _ = make()
    run = run_pipeline(orch)
    gh.pr_state[1] = {"state": "closed", "merged": True}
    asyncio.run(orch.sync_prs())
    assert run.status == "merged" and run.steps[-1].agent == "Human"


def test_demo_break_and_reset_endpoints():
    orch, gh, _ = make()
    orch.s.allowed_repos, orch.s.demo_controls = ["me/demo-app"], True
    with client_with(orch) as c:
        cfg = c.get("/api/config").json()
        assert [b["id"] for b in cfg["demo_bugs"]] == ["test", "dep", "lint"]
        assert c.post("/api/demo/break", json={"bug": "test"}).status_code == 200
        assert "if n > 0:" in gh.default_branch["src/shop/cart.py"]
        assert c.post("/api/demo/break", json={"bug": "test"}).status_code == 409  # already broken
        assert c.post("/api/demo/break", json={"bug": "dep"}).status_code == 200
        assert "2.99.0" in gh.default_branch["requirements.txt"]
        assert c.post("/api/demo/break", json={"bug": "x", "repo": "evil/repo"}).status_code == 400
        r = c.post("/api/demo/reset", json={}).json()
        assert set(r["changed"]) >= {"src/shop/cart.py", "requirements.txt"}
        orch.s.demo_controls = False
        assert c.post("/api/demo/break", json={"bug": "lint"}).status_code == 403
