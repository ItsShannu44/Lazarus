"""Thin async wrapper over the GitHub REST API: only the calls Lazarus needs."""
from __future__ import annotations

import base64
from typing import Any

import httpx


HINTS = {
    401: "GitHub token is invalid or expired; create a new one and update GITHUB_TOKEN",
    403: "GitHub token is missing a permission (Actions, Contents, Pull requests or Issues)",
    404: "repository, run or file not found, or the token has no access to this repository",
    409: "conflict on GitHub (branch changed or repository is empty)",
    422: "GitHub rejected the request (branch exists, nothing to commit, or invalid PR)",
}


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.hint = HINTS.get(status, "")
        detail = message[:300]
        super().__init__(f"GitHub API {status}: {self.hint + ' | ' if self.hint else ''}{detail}")


class GitHub:
    def __init__(self, token: str, base_url: str = "https://api.github.com"):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lazarus-ci-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # follow_redirects: log downloads 302 to blob storage. httpx drops the
        # Authorization header on cross-origin redirects, which is what we want.
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, follow_redirects=True
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        r = await self._http.request(method, url, **kw)
        if r.status_code >= 400:
            raise GitHubError(r.status_code, r.text)
        return r

    # ---------- CI runs & logs ----------
    async def get_run(self, repo: str, run_id: int) -> dict:
        return (await self._req("GET", f"/repos/{repo}/actions/runs/{run_id}")).json()

    async def latest_failed_run(self, repo: str, branch: str | None = None) -> dict | None:
        params: dict[str, Any] = {"status": "failure", "per_page": 1}
        if branch:
            params["branch"] = branch
        runs = (await self._req("GET", f"/repos/{repo}/actions/runs", params=params)).json()
        items = runs.get("workflow_runs", [])
        return items[0] if items else None

    async def runs_for_sha(self, repo: str, sha: str) -> list[dict]:
        r = await self._req(
            "GET", f"/repos/{repo}/actions/runs", params={"head_sha": sha, "per_page": 20}
        )
        return r.json().get("workflow_runs", [])

    async def list_repos(self, max_pages: int = 3) -> list[dict]:
        """Every repository this token can see (for fine-grained tokens: the ones selected on the token)."""
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            r = await self._req("GET", "/user/repos", params={"per_page": 100, "page": page, "sort": "pushed"})
            batch = r.json()
            out += batch
            if len(batch) < 100:
                break
        return out

    async def get_repo(self, repo: str) -> dict:
        return (await self._req("GET", f"/repos/{repo}")).json()

    async def list_workflows(self, repo: str) -> list[dict]:
        r = await self._req("GET", f"/repos/{repo}/actions/workflows", params={"per_page": 100})
        return r.json().get("workflows", [])

    async def recent_runs(self, repo: str, per_page: int = 15) -> list[dict]:
        r = await self._req("GET", f"/repos/{repo}/actions/runs", params={"per_page": per_page})
        return r.json().get("workflow_runs", [])

    async def runs_for_branch(self, repo: str, branch: str, per_page: int = 20) -> list[dict]:
        r = await self._req("GET", f"/repos/{repo}/actions/runs", params={"branch": branch, "per_page": per_page})
        return r.json().get("workflow_runs", [])

    async def open_prs(self, repo: str) -> list[dict]:
        r = await self._req("GET", f"/repos/{repo}/pulls", params={"state": "open", "per_page": 50})
        return r.json()

    async def all_jobs(self, repo: str, run_id: int) -> list[dict]:
        r = await self._req(
            "GET", f"/repos/{repo}/actions/runs/{run_id}/jobs", params={"filter": "latest", "per_page": 50}
        )
        return r.json().get("jobs", [])

    async def failed_jobs(self, repo: str, run_id: int) -> list[dict]:
        r = await self._req(
            "GET",
            f"/repos/{repo}/actions/runs/{run_id}/jobs",
            params={"filter": "latest", "per_page": 50},
        )
        jobs = r.json().get("jobs", [])
        return [j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled")]

    async def job_logs(self, repo: str, job_id: int) -> str:
        r = await self._req("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        return r.text

    # ---------- repository contents ----------
    async def get_tree(self, repo: str, sha: str) -> list[dict]:
        """Every file at commit `sha`: [{path, mode, sha, size}]."""
        r = await self._req("GET", f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"})
        return [t for t in r.json().get("tree", []) if t.get("type") == "blob"]

    async def get_file(self, repo: str, path: str, ref: str) -> str:
        r = await self._req("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
        data = r.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return data.get("content", "")

    async def get_contents(self, repo: str, path: str) -> tuple[str, str]:
        """(text, blob sha) on the default branch."""
        data = (await self._req("GET", f"/repos/{repo}/contents/{path}")).json()
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

    async def put_contents(
        self, repo: str, path: str, text: str, sha: str | None, message: str, branch: str | None = None
    ) -> None:
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(text.encode()).decode()}
        if sha:
            body["sha"] = sha  # updating an existing file
        if branch:
            body["branch"] = branch
        await self._req("PUT", f"/repos/{repo}/contents/{path}", json=body)

    # ---------- writing a fix (one commit, one CI run) ----------
    async def commit_files(
        self, repo: str, base_sha: str, files: dict[str, tuple[str, str]], message: str
    ) -> str:
        """Create a single commit on top of base_sha. files = {path: (content, mode)}."""
        base_commit = (await self._req("GET", f"/repos/{repo}/git/commits/{base_sha}")).json()
        tree_entries = [
            {"path": p, "mode": mode or "100644", "type": "blob", "content": content}
            for p, (content, mode) in files.items()
        ]
        tree = (
            await self._req(
                "POST",
                f"/repos/{repo}/git/trees",
                json={"base_tree": base_commit["tree"]["sha"], "tree": tree_entries},
            )
        ).json()
        commit = (
            await self._req(
                "POST",
                f"/repos/{repo}/git/commits",
                json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
            )
        ).json()
        return commit["sha"]

    async def create_branch(self, repo: str, branch: str, sha: str) -> None:
        await self._req(
            "POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}
        )

    async def can_write(self, repo: str) -> bool | None:
        """Can this token push to `repo`? Asks GitHub to create a branch at an impossible commit:
        422 (bad commit) means the write was allowed; 403/404 means it was refused. Nothing is created."""
        probe = "lazarus/access-check"
        try:
            await self._req("POST", f"/repos/{repo}/git/refs",
                            json={"ref": f"refs/heads/{probe}", "sha": "0" * 40})
        except GitHubError as e:
            if e.status == 422:
                return True
            if e.status in (403, 404):
                return False
            return None
        except httpx.HTTPError:
            return None
        try:  # should never happen, but never leave a stray branch
            await self.delete_branch(repo, probe)
        except Exception:
            pass
        return True

    async def delete_branch(self, repo: str, branch: str) -> None:
        await self._req("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")

    # ---------- pull requests ----------
    async def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        r = await self._req(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        return r.json()

    async def merge_pr(self, repo: str, number: int, method: str = "squash") -> dict:
        r = await self._req(
            "PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": method}
        )
        return r.json()

    async def update_pr(self, repo: str, number: int, body: str) -> None:
        await self._req("PATCH", f"/repos/{repo}/pulls/{number}", json={"body": body})

    async def close_pr(self, repo: str, number: int) -> None:
        await self._req("PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})

    async def branch_head(self, repo: str, branch: str) -> str:
        r = await self._req("GET", f"/repos/{repo}/branches/{branch}")
        return r.json()["commit"]["sha"]

    async def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        await self._req("POST", f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs")

    async def get_pr(self, repo: str, number: int) -> dict:
        return (await self._req("GET", f"/repos/{repo}/pulls/{number}")).json()

    async def create_issue(self, repo: str, title: str, body: str) -> dict:
        r = await self._req("POST", f"/repos/{repo}/issues", json={"title": title, "body": body})
        return r.json()

    async def comment(self, repo: str, number: int, body: str) -> None:
        await self._req(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
        )
