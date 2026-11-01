"""Recoverable problems park the run ("waiting") and Lazarus finishes it by itself."""
import asyncio

from app.github_client import GitHubError
from app.llm import LLMError

from .fakes import FakeLLM
from .test_pipeline import make


def healing(**kw):
    orch, gh, llm = make(**kw)
    orch.s.auto_heal, orch.s.auto_retries, orch.s.heal_delay_s, orch.s.no_run_grace_s = True, 2, 0, 0
    return orch, gh, llm


def run_then_heal(orch, before_heal=None, rounds=3):
    async def go():
        run = await orch.start_manual("me/demo-app", 555)
        await asyncio.gather(*orch._tasks)
        if before_heal:
            before_heal(run)
        for _ in range(rounds):
            if run.status != "waiting":
                break
            run.retry_at = 0
            await orch.heal_once()
            await asyncio.gather(*orch._tasks)
        return run
    return asyncio.run(go())


def test_read_only_token_waits_then_finishes_by_itself():
    orch, gh, _ = healing()
    real_commit, real_put = gh.commit_files, gh.put_contents

    async def refused(*a, **k):
        raise GitHubError(403, "Resource not accessible by personal access token")
    gh.commit_files = gh.put_contents = refused
    gh.writable = False
    seen = {}

    def grant(run):
        seen["status"], seen["kind"] = run.status, run.wait_kind
        gh.commit_files, gh.put_contents = real_commit, real_put

    async def go():
        run = await orch.start_manual("me/demo-app", 555)
        await asyncio.gather(*orch._tasks)
        grant(run)
        run.retry_at = 0
        await orch.heal_once()                     # token still read-only
        assert run.status == "waiting" and run.resumes == 0
        gh.writable = True                         # user adds the repo to the token
        run.retry_at = 0
        await orch.heal_once()
        await asyncio.gather(*orch._tasks)
        return run
    run = asyncio.run(go())
    assert seen == {"status": "waiting", "kind": "access"}
    assert run.status == "awaiting_approval" and gh.prs, run.escalation_reason
    assert not gh.issues  # nobody was paged


def test_rate_limit_is_retried_automatically():
    class FlakyLLM(FakeLLM):
        failed = False

        async def json(self, system, prompt, schema, agent=""):
            if not self.failed:
                self.failed = True
                raise LLMError("Every LLM model hit its free-tier rate limit.")
            return await super().json(system, prompt, schema, agent)

    orch, gh, _ = healing()
    orch.llm = FlakyLLM()
    run = run_then_heal(orch)
    assert run.status == "awaiting_approval" and run.resumes == 1


def test_red_ci_gets_fresh_tries_then_a_human():
    orch, gh, _ = healing(ci_outcomes=["failure"] * 20)
    run = run_then_heal(orch, rounds=5)
    assert run.status == "needs_human" and "automatic retries" in run.escalation_reason
    assert run.resumes == 2


def test_ci_that_only_runs_on_pull_requests_still_verifies():
    orch, gh, _ = healing()
    gh.ci_only_on_pr = True
    run = run_then_heal(orch)
    assert run.status == "awaiting_approval", run.escalation_reason
    assert len(gh.prs) == 1 and gh.prs[1]["head"] == run.fix_branch  # the early PR is the final PR
    assert "CI didn't start on the push" in " ".join(s.message for s in run.steps)


def test_early_pr_is_closed_when_that_attempt_fails():
    orch, gh, _ = healing(ci_outcomes=["failure", "success"])
    gh.ci_only_on_pr = True
    run = run_then_heal(orch)
    assert run.status == "awaiting_approval"
    assert gh.prs[1]["state"] == "closed" and gh.prs[2]["state"] == "open"
    assert run.pr_number == 2


def test_permanent_problems_still_go_to_a_human():
    orch, gh, _ = healing()
    gh.files["src/shop/cart.py"] += "�"
    run = run_then_heal(orch)
    assert run.status == "needs_human" and "non-UTF-8" in run.escalation_reason


def test_retry_endpoint_restarts_a_stopped_run():
    from .test_pipeline import client_with
    orch, gh, _ = make(ci_outcomes=["failure"] * 3)
    run = run_then_heal(orch)
    assert run.status == "needs_human"
    gh.ci_outcomes = ["success"]
    with client_with(orch) as c:
        r = c.post(f"/api/runs/{run.id}/retry")
        assert r.status_code == 200
        for _ in range(100):
            if run.status == "awaiting_approval":
                break
            c.get("/health")
    assert run.status == "awaiting_approval", run.escalation_reason
