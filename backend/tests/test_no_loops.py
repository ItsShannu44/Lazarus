"""Lazarus must never re-fix a failure that is already fixed (the 'loop' bug)."""
import asyncio
import time

import pytest

from app.orchestrator import Orchestrator
from app.store import Store

from .fakes import failed_run
from .test_pipeline import make, run_pipeline


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def ci(run_id, conclusion, t, branch="main", sha=None, workflow_id=1):
    return failed_run(run_id) | {
        "conclusion": conclusion, "status": "completed", "head_branch": branch,
        "head_sha": sha or f"sha{run_id}", "workflow_id": workflow_id,
        "created_at": iso(t), "updated_at": iso(t), "run_number": run_id,
    }


def history_after_merge(now):
    """Failure F (fixed by Lazarus), then the merge commit's CI went green. Newest first, like GitHub."""
    return [ci(11, "success", now + 30, sha="merged"), ci(10, "failure", now + 5, sha="broken")]


def test_watch_after_restart_does_not_refix_old_failure():
    orch, gh, _ = make()
    orch.s.allowed_repos = ["me/demo-app"]
    now = time.time()
    orch.started_at = now - 600  # e.g. PC clock behind GitHub, or restarted just before
    gh.recent = history_after_merge(now)
    started = asyncio.run(orch.poll_once())
    assert started == [], "re-fixed a failure that a newer green run already fixed"


def test_run_on_latest_failure_refuses_when_branch_is_green_again():
    orch, gh, _ = make()
    now = time.time()
    gh.recent = history_after_merge(now)

    async def latest_failed(repo, branch=None):
        return gh.recent[1]
    gh.latest_failed_run = latest_failed
    with pytest.raises(ValueError, match="already fixed|passed"):
        asyncio.run(orch.start_manual("me/demo-app"))


def test_stale_failure_stands_down_before_spending_llm_calls():
    orch, gh, llm = make()
    now = time.time()
    gh.recent = history_after_merge(now)
    run = run_pipeline_on(orch, gh.recent[1])
    assert run.status == "superseded" and not llm.calls and not gh.prs


def test_open_lazarus_pr_for_same_failure_blocks_duplicate_after_restart():
    orch, gh, llm = make()
    gh.open_prs_list = [{"number": 5, "html_url": "https://github.com/me/demo-app/pull/5",
                    "head": {"ref": "lazarus/run-3-abc-a1"},
                    "body": "**Failed run:** https://github.com/me/demo-app/actions/runs/555"}]
    run = run_pipeline(orch)  # run id 555
    assert run.status == "superseded" and "#5" in run.escalation_reason and not llm.calls


def test_new_failure_after_green_is_still_fixed():
    orch, gh, _ = make()
    orch.s.allowed_repos = ["me/demo-app"]
    now = time.time()
    orch.started_at = now
    gh.recent = [ci(12, "failure", now + 60, sha="broken-again")] + history_after_merge(now)

    async def go():
        started = await orch.poll_once()
        await asyncio.gather(*orch._tasks)
        return started
    started = asyncio.run(go())
    assert [r.ci_run_id for r in started] == [12] and started[0].status == "awaiting_approval"


def test_other_workflows_and_branches_are_judged_separately():
    orch, gh, _ = make()
    orch.s.allowed_repos = ["me/demo-app"]
    now = time.time()
    orch.started_at = now
    gh.recent = [
        ci(21, "success", now + 50, workflow_id=2),            # a different workflow passed
        ci(20, "failure", now + 40, workflow_id=1),            # CI on main still red -> fix
        ci(31, "failure", now + 45, branch="feature", sha="f1"),  # another branch red -> fix
    ]

    async def go():
        started = await orch.poll_once()
        await asyncio.gather(*orch._tasks)
        return sorted(r.ci_run_id for r in started)
    assert asyncio.run(go()) == [20, 31]


def test_seen_keys_survive_restart_with_mongo_format():
    from app.models import Run
    r = Run(repo="me/demo-app", ci_run_id=10, run_attempt=1, status="merged", number=1)

    class Cursor:
        def sort(self, *a): return self
        def limit(self, *a): return self
        def __aiter__(self):
            async def gen():
                yield r.model_dump()
            return gen()

    class Col:
        def find(self): return Cursor()
        async def replace_one(self, *a, **k): pass

    store = Store()
    store._col = Col()
    asyncio.run(store.load())
    orch = Orchestrator(make()[0].s, make()[1], make()[2], store)
    assert orch.start("me/demo-app", failed_run(10)) is None


def run_pipeline_on(orch, wr):
    async def go():
        run = orch.start("me/demo-app", wr, trigger="watch")
        await asyncio.gather(*orch._tasks)
        return run
    return asyncio.run(go())


def test_manual_trigger_explains_missing_or_green_ci():
    orch, gh, _ = make()
    gh.recent = []
    with pytest.raises(ValueError, match="no GitHub Actions runs"):
        asyncio.run(orch.start_manual("me/js-core"))
    gh.recent = [ci(5, "success", time.time())]
    with pytest.raises(ValueError, match="CI isn't checking for it"):
        asyncio.run(orch.start_manual("me/js-core"))
    gh.recent = [failed_run(6) | {"status": "in_progress", "conclusion": None, "head_branch": "main"}]
    with pytest.raises(ValueError, match="still running"):
        asyncio.run(orch.start_manual("me/js-core"))
