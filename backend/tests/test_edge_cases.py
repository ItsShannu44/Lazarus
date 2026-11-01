"""One test per edge case Lazarus claims to handle."""
import asyncio

import pytest

from app.github_client import GitHubError
from app.models import Run
from app.store import Store
from app.textutil import cheat_reason, norm_path, redact_secrets

from .fakes import CART_BROKEN, CI_LOG, FakeLLM, failed_run
from .test_pipeline import client_with, make, run_pipeline

GOOD_FIX = FakeLLM().fixes[0]


def fix(path, search, replace, conf=90):
    return {"reasoning": "r", "confidence": conf, "edits": [{"path": path, "search": search, "replace": replace}]}


class AnalystSays(FakeLLM):
    def __init__(self, error_type, **kw):
        super().__init__(**kw)
        self.error_type = error_type

    async def json(self, system, prompt, schema, agent=""):
        out = await super().json(system, prompt, schema, agent)
        if agent == "Log Analyst":
            out["error_type"] = self.error_type
        return out


# ------------------------------------------------------------------ 1. cheating fixes
@pytest.mark.parametrize("bad_replace", [
    "    if n > 0:  # noqa",
    "    if n > 0:  # type: ignore",
])
def test_fixer_cannot_silence_checks(bad_replace):
    orch, gh, llm = make(fixes=[fix("src/shop/cart.py", "    if n > 0:", bad_replace), GOOD_FIX])
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval" and "if n >= 0:" in run.diff
    assert "silences a linter" in [p for a, p in llm.calls if a == "Fixer"][-1]


def test_fixer_cannot_weaken_or_skip_tests():
    test_src = "def test_check_allows_zero():\n    assert check(0)\n"
    assert "removes test assertions" in cheat_reason("tests/test_cart.py", test_src, "def test_check_allows_zero():\n    pass\n")
    assert "skips a test" in cheat_reason(
        "tests/test_cart.py", test_src, "import pytest\n@pytest.mark.skip\n" + test_src)
    assert "deletes a test" in cheat_reason("tests/test_cart.py", test_src + "def test_b():\n    assert 1\n", test_src + "\n")
    assert cheat_reason("src/shop/cart.py", "if n > 0:", "if n >= 0:") is None  # honest fix passes
    assert "skips a test" in cheat_reason("src/a.test.ts", "it('x', () => {})", "it.skip('x', () => {})")


def test_cheating_every_time_escalates():
    orch, gh, _ = make(fixes=[fix("tests/test_cart.py", "    assert check(0)\n", "    pass\n")])
    run = run_pipeline(orch)
    assert run.status == "needs_human" and "removes test assertions" in run.escalation_reason
    assert not gh.commits


# ------------------------------------------------------------------ 2. secrets never leave
def test_secrets_are_redacted_from_logs_prompts_and_issues():
    leaked = "ghp_" + "a" * 36
    text = f"token={leaked}\nAuthorization: Bearer {'x' * 30}\nGEMINI AIza{'B' * 35}\nmongodb+srv://u:p@c.net/db"
    out = redact_secrets(text)
    assert leaked not in out and "AIza" not in out and "u:p@" not in out and out.count("[REDACTED]") >= 4

    orch, gh, llm = make(ci_outcomes=["failure"] * 3)
    orch.s.escalate_issue = True

    async def leaky_logs(repo, job_id):
        return CI_LOG + f"\nexport GITHUB_TOKEN={leaked}\n"
    gh.job_logs = leaky_logs
    run = run_pipeline(orch)
    assert leaked not in run.log_excerpt
    assert all(leaked not in p for _, p in llm.calls)
    assert leaked not in gh.issues[0]["body"]


# ------------------------------------------------------------------ 3. someone pushed meanwhile
def test_stand_down_when_newer_commit_is_already_green():
    orch, gh, _ = make()
    gh.head_now = "newsha"
    gh.commits["newsha"] = {"outcome": "success", "files": {}, "message": ""}
    run = run_pipeline(orch)
    assert run.status == "superseded" and not gh.prs
    assert "already made CI green" in run.escalation_reason
    assert run.fix_branch in gh.deleted


def test_branch_moved_but_still_red_still_opens_pr():
    orch, gh, _ = make()
    gh.head_now = "redsha"
    gh.commits["redsha"] = {"outcome": "failure", "files": {}, "message": ""}
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval" and gh.prs
    assert any("may need a rebase" in s.message for s in run.steps)


def test_branch_deleted_while_working():
    orch, gh, _ = make()
    gh.branch_deleted = True
    run = run_pipeline(orch)
    assert run.status == "superseded" and "was deleted" in run.escalation_reason


# ------------------------------------------------------------------ 4. flaky / infra
def test_infra_failure_reruns_ci_instead_of_patching():
    orch, gh, _ = make()
    orch.llm = AnalystSays("infra")
    run = run_pipeline(orch)
    assert run.status == "rerun" and gh.reruns == [555] and not gh.commits


def test_infra_failure_twice_escalates():
    orch, gh, _ = make()
    orch.llm = AnalystSays("infra")

    async def attempt2(repo, run_id):
        return failed_run(run_id) | {"run_attempt": 2}
    gh.get_run = attempt2
    run = run_pipeline(orch)
    assert run.status == "needs_human" and "again after a re-run" in run.escalation_reason


def test_infra_rerun_without_permission_explains():
    orch, gh, _ = make()
    orch.llm = AnalystSays("infra")
    gh.rerun_forbidden = True
    run = run_pipeline(orch)
    assert run.status == "needs_human" and "Actions: Read and write" in run.escalation_reason


# ------------------------------------------------------------------ 5. many failures at once
def test_concurrency_limit_queues_extra_runs():
    class SlowLLM(FakeLLM):
        async def json(self, *a, **k):
            await asyncio.sleep(0.01)
            return await super().json(*a, **k)

    orch, gh, _ = make()
    orch.llm = SlowLLM()
    orch.s.max_concurrent = 1
    orch._slots = asyncio.Semaphore(1)

    async def go():
        a = orch.start("me/demo-app", failed_run(1) | {"head_sha": "s1"})
        b = orch.start("me/demo-app", failed_run(2) | {"head_sha": "s2"})
        await asyncio.gather(*orch._tasks)
        return a, b
    a, b = asyncio.run(go())
    assert a.status == b.status == "awaiting_approval"
    assert any("Queued" in s.message for s in b.steps)


# ------------------------------------------------------------------ 6. restart mid-run
def test_runs_interrupted_by_restart_are_marked():
    stuck = Run(repo="me/demo-app", ci_run_id=9, status="verifying", number=4)
    done = Run(repo="me/demo-app", ci_run_id=8, status="merged", number=3)

    class Cursor:
        def __init__(self, docs): self.docs = docs
        def sort(self, *a): return self
        def limit(self, *a): return self
        def __aiter__(self):
            async def gen():
                for d in self.docs:
                    yield d
            return gen()

    class Col:
        saved = []
        def find(self): return Cursor([stuck.model_dump(), done.model_dump()])
        async def replace_one(self, q, doc, upsert): self.saved.append(doc)

    store = Store()
    store._col = Col()
    asyncio.run(store.load())
    assert store.runs[stuck.id].status == "error" and "restart" in store.runs[stuck.id].escalation_reason
    assert store.runs[done.id].status == "merged" and store._counter == 4


# ------------------------------------------------------------------ 7. merge conflict on approve
def test_merge_conflict_gives_clear_message():
    orch, gh, _ = make()
    run = run_pipeline(orch)

    async def conflict(*a, **k):
        raise GitHubError(405, "Pull Request is not mergeable")
    gh.merge_pr = conflict
    with client_with(orch) as c:
        r = c.post(f"/api/runs/{run.id}/approve")
    assert r.status_code == 409 and "can't merge PR #1 automatically" in r.json()["detail"]
    assert run.status == "awaiting_approval"  # still approvable after fixing on GitHub


# ------------------------------------------------------------------ 8. token problems
def test_token_errors_explain_the_fix():
    assert "invalid or expired" in str(GitHubError(401, "Bad credentials"))
    orch, gh, _ = make()

    async def forbidden(*a, **k):
        raise GitHubError(403, "Resource not accessible by personal access token")
    gh.get_tree = forbidden
    run = run_pipeline(orch)
    assert run.status == "needs_human" and "missing a permission" in run.escalation_reason


# ------------------------------------------------------------------ 9. binary / non-UTF-8
def test_refuses_to_rewrite_non_utf8_file():
    orch, gh, _ = make()
    gh.files["src/shop/cart.py"] = CART_BROKEN + "��"
    run = run_pipeline(orch)
    assert run.status == "needs_human" and "non-UTF-8" in run.escalation_reason and not gh.commits


# ------------------------------------------------------------------ 10. broken workflow file
def test_invalid_workflow_is_handed_to_human():
    orch, gh, llm = make()

    async def no_jobs(repo, run_id):
        return []
    gh.failed_jobs = no_jobs
    run = run_pipeline(orch)
    assert run.status == "needs_human" and "workflow file itself is likely invalid" in run.escalation_reason
    assert not llm.calls  # no LLM tokens spent


# ------------------------------------------------------------------ 11. CI re-run that fails again
def test_rerun_attempt_is_a_new_incident():
    orch, gh, _ = make()

    async def go():
        a = orch.start("me/demo-app", failed_run(77))
        await asyncio.gather(*orch._tasks)
        dup = orch.start("me/demo-app", failed_run(77))
        b = orch.start("me/demo-app", failed_run(77) | {"run_attempt": 2})
        await asyncio.gather(*orch._tasks)
        return a, dup, b
    a, dup, b = asyncio.run(go())
    assert a and dup is None and b and b.run_attempt == 2


# ------------------------------------------------------------------ paths
def test_paths_keep_dotfiles_and_workflows_stay_blocked():
    assert norm_path("./src/x.py") == "src/x.py" and norm_path("/src/x.py") == "src/x.py"
    assert norm_path(".github/workflows/ci.yml") == ".github/workflows/ci.yml"
    assert norm_path(".gitignore") == ".gitignore"
    orch, gh, llm = make(fixes=[fix(".github/workflows/ci.yml", "on: [push]", "on: []"), GOOD_FIX])
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval"
    assert "editing CI workflows is not allowed" in [p for a, p in llm.calls if a == "Fixer"][-1]


# ------------------------------------------------------------------ token can read but not write
def test_push_falls_back_to_contents_api():
    orch, gh, _ = make()

    async def refused(*a, **k):
        raise GitHubError(403, "Resource not accessible by personal access token")
    gh.commit_files = refused
    puts = []

    async def put(repo, path, text, sha, message, branch=None):
        puts.append((path, branch))
        gh.commits["c-fallback"] = {"outcome": "success", "files": {path: (text, "100644")}, "message": message}
    gh.put_contents = put

    async def head(repo, branch):
        return "c-fallback" if branch.startswith("lazarus/") else "abc123"
    gh.branch_head = head
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval", run.escalation_reason
    assert puts and puts[0][0] == "src/shop/cart.py" and puts[0][1].startswith("lazarus/")


def test_read_only_token_is_explained_and_fix_is_kept():
    orch, gh, _ = make()

    async def refused(*a, **k):
        raise GitHubError(403, "Resource not accessible by personal access token")
    gh.commit_files = refused
    gh.put_contents = refused
    run = run_pipeline(orch)
    assert run.status == "needs_human"
    assert "can read me/demo-app and not write to it" in run.escalation_reason
    assert "if n >= 0:" in run.diff  # the proposed fix is still shown to the human
