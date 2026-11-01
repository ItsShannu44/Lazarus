"""In-memory stand-ins for GitHub and the LLM so the whole pipeline runs offline."""
from __future__ import annotations

import itertools

CART_BROKEN = '''def check(n: int) -> bool:
    """Quantities may be zero (item kept in cart) but never negative."""
    if n > 0:
        return True
    return False
'''

CI_LOG = """2026-10-31T10:00:00.0000000Z ##[group]Run pytest -q
2026-10-31T10:00:01.0000000Z \x1b[31mFAILED\x1b[0m tests/test_cart.py::test_check_allows_zero - assert False
2026-10-31T10:00:01.0000000Z E       assert False
2026-10-31T10:00:01.0000000Z  +  where False = check(0)
2026-10-31T10:00:01.0000000Z /home/runner/work/demo-app/demo-app/src/shop/cart.py:15: AssertionError
2026-10-31T10:00:02.0000000Z ##[error]Process completed with exit code 1.
"""


GREEN_LOG = """2026-10-31T10:05:00.0000000Z ##[group]Run pytest -q
2026-10-31T10:05:01.0000000Z ......                                                   [100%]
2026-10-31T10:05:01.0000000Z 6 passed in 0.03s
"""


class FakeGitHub:
    def __init__(self, ci_outcomes: list[str] | None = None):
        self.files = {
            "src/shop/cart.py": CART_BROKEN,
            "tests/test_cart.py": "def test_check_allows_zero():\n    assert check(0)\n",
            "README.md": "# demo",
            ".github/workflows/ci.yml": "on: [push]",
        }
        self.ci_outcomes = list(ci_outcomes or ["success"])  # one per fix attempt
        self.commits: dict[str, dict] = {}
        self.branches: dict[str, str] = {}
        self.prs: dict[int, dict] = {}
        self.deleted: list[str] = []
        self.merged: list[int] = []
        self._ids = itertools.count(1000)
        self.issues: list[dict] = []
        self.recent: list[dict] = []          # what recent_runs() returns
        self.pr_state: dict[int, dict] = {}   # overrides for get_pr()
        self.default_branch = {"src/shop/cart.py": CART_BROKEN.replace("n > 0", "n >= 0"),
                               "requirements.txt": "pytest==8.3.3\nruff==0.6.9\nrequests==2.32.3\n",
                               "src/shop/pricing.py": "from __future__ import annotations\n"}
        self.contents_commits: list[str] = []

    async def get_run(self, repo, run_id):
        return failed_run(run_id)

    async def latest_failed_run(self, repo, branch=None):
        return failed_run(555)

    async def failed_jobs(self, repo, run_id):
        return [{"id": run_id * 10, "name": "test", "conclusion": "failure",
                 "steps": [{"name": "Unit tests", "conclusion": "failure"}]}]

    async def job_logs(self, repo, job_id):
        return GREEN_LOG if job_id == 4242 else CI_LOG

    async def all_jobs(self, repo, run_id):
        return [{"id": 4242, "name": "test", "conclusion": "success", "steps": []}]

    async def recent_runs(self, repo, per_page=15):
        self.polled = getattr(self, "polled", []) + [repo]
        return self.recent_for(repo)

    async def get_repo(self, repo):
        return {"full_name": repo, "default_branch": "main"}

    async def list_workflows(self, repo):
        return getattr(self, "workflows", [])

    async def list_repos(self, max_pages=3):
        return getattr(self, "repos", [])

    def recent_for(self, repo):
        return getattr(self, "recent_by_repo", {}).get(repo, self.recent)

    async def runs_for_branch(self, repo, branch, per_page=20):
        return [r for r in self.recent if r.get("head_branch") == branch]

    async def open_prs(self, repo):
        return getattr(self, "open_prs_list", [])

    async def branch_head(self, repo, branch):
        if getattr(self, "branch_deleted", False):
            from app.github_client import GitHubError
            raise GitHubError(404, "Branch not found")
        return getattr(self, "head_now", "abc123")

    async def rerun_failed_jobs(self, repo, run_id):
        if getattr(self, "rerun_forbidden", False):
            from app.github_client import GitHubError
            raise GitHubError(403, "Resource not accessible by personal access token")
        self.reruns = getattr(self, "reruns", []) + [run_id]

    async def get_pr(self, repo, number):
        return self.pr_state.get(number, {"state": "open", "merged": False})

    async def create_issue(self, repo, title, body):
        self.issues.append({"title": title, "body": body})
        return {"number": len(self.issues), "html_url": f"https://github.com/{repo}/issues/{len(self.issues)}"}

    async def get_contents(self, repo, path):
        return self.default_branch[path], "sha-" + path

    async def put_contents(self, repo, path, text, sha, message, branch=None):
        if getattr(self, 'contents_forbidden', False):
            from app.github_client import GitHubError
            raise GitHubError(404, 'Not Found')
        self.contents_branch = branch
        self.default_branch[path] = text
        self.contents_commits.append(message)

    async def get_tree(self, repo, sha):
        return [{"path": p, "mode": "100644", "size": len(c), "type": "blob"} for p, c in self.files.items()]

    async def get_file(self, repo, path, ref):
        return self.files[path]

    async def commit_files(self, repo, base_sha, files, message):
        sha = f"c{next(self._ids)}"
        self.commits[sha] = {"files": files, "message": message,
                             "outcome": self.ci_outcomes.pop(0) if self.ci_outcomes else "success"}
        return sha

    async def create_branch(self, repo, branch, sha):
        self.branches[branch] = sha

    async def can_write(self, repo):
        return getattr(self, "writable", True)

    async def delete_branch(self, repo, branch):
        self.deleted.append(branch)

    async def runs_for_sha(self, repo, sha):
        if getattr(self, "ci_only_on_pr", False) and not any(
                self.branches.get(pr["head"]) == sha for pr in self.prs.values() if pr["state"] == "open"):
            return []  # workflow is `on: pull_request` only: nothing runs on a bare push
        c = self.commits[sha]
        return [{"id": next(self._ids), "status": "completed", "conclusion": c["outcome"],
                 "html_url": f"https://github.com/x/runs/{sha}"}]

    async def create_pr(self, repo, head, base, title, body):
        n = len(self.prs) + 1
        self.prs[n] = {"head": head, "base": base, "title": title, "body": body, "state": "open"}
        return {"number": n, "html_url": f"https://github.com/{repo}/pull/{n}"}

    async def update_pr(self, repo, number, body):
        self.prs[number]["body"] = body

    async def merge_pr(self, repo, number, method="squash"):
        self.merged.append(number)
        return {"merged": True}

    async def close_pr(self, repo, number):
        self.prs[number]["state"] = "closed"


def failed_run(run_id):
    return {"id": run_id, "name": "CI", "head_sha": "abc123", "head_branch": "main",
            "conclusion": "failure", "status": "completed",
            "html_url": f"https://github.com/me/demo-app/actions/runs/{run_id}"}


class FakeLLM:
    """Answers per agent. Fixer answers are consumed in order."""

    def __init__(self, fixes: list[dict] | None = None):
        good = {
            "reasoning": "check() uses > so 0 is rejected; spec says zero is allowed.",
            "confidence": 87,
            "edits": [{"path": "src/shop/cart.py", "search": "    if n > 0:", "replace": "    if n >= 0:"}],
        }
        self.fixes = list(fixes or [good])
        self.calls: list[tuple[str, str]] = []

    async def json(self, system, prompt, schema, agent=""):
        self.calls.append((agent, prompt))
        if agent == "Log Analyst":
            return {"error_type": "test_failure", "summary": "test_check_allows_zero fails: check(0) is False",
                    "failing_test": "test_check_allows_zero", "root_cause_hypothesis": "off-by-one in check()",
                    "suspected_files": ["src/shop/cart.py"], "key_lines": []}
        if agent == "Code Locator":
            return {"files": [{"path": "src/shop/cart.py", "reason": "defines check()"},
                              {"path": "does/not/exist.py", "reason": "hallucinated"}]}
        if agent == "Fixer":
            return self.fixes.pop(0) if len(self.fixes) > 1 else self.fixes[0]
        raise AssertionError(agent)
