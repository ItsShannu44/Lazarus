"""Give a repository a CI workflow so Lazarus can see its failures.

Lazarus only wakes up when GitHub Actions goes red. A repo without CI (or with CI that never
checks the code) can be full of bugs and stay green. This module looks at the repo's files,
picks checks that catch real bugs without failing on style, and proposes the workflow as a PR.

Python : install deps, ruff with error-only rules (syntax errors, undefined names), compileall,
         pytest when tests exist.
JS     : `node --check` every .js/.mjs/.cjs file (ES modules checked as modules),
         `npm test` when package.json defines one.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from .github_client import GitHub, GitHubError

WORKFLOW_PATH = ".github/workflows/lazarus-ci.yml"
SKIP_DIRS = ("node_modules/", ".venv/", "venv/", "dist/", "build/", ".next/", "vendor/", "site-packages/")


def detect(paths: list[str], package_json: str = "") -> dict:
    files = [p for p in paths if not any(f"/{d}" in f"/{p}" for d in SKIP_DIRS)]
    names = {p.split("/")[-1] for p in files}
    py = [p for p in files if p.endswith(".py")]
    js = [p for p in files if p.endswith((".js", ".mjs", ".cjs"))]
    has_tests_py = any(re.search(r"(^|/)(tests?/.*\.py|test_[^/]*\.py|[^/]*_test\.py)$", p) for p in py)
    npm_test = bool(re.search(r'"test"\s*:\s*"(?!echo \\?"Error: no test specified)', package_json))
    return {
        "python": bool(py),
        "javascript": bool(js),
        "requirements": "requirements.txt" in names and "requirements.txt" in files,
        "pyproject": "pyproject.toml" in files,
        "python_tests": has_tests_py,
        "package_json": "package.json" in files,
        "npm_test": npm_test,
        "py_files": len(py),
        "js_files": len(js),
    }


def build_workflow(info: dict) -> str:
    jobs: list[str] = []
    if info["python"]:
        install = []
        if info["requirements"]:
            install.append("pip install -r requirements.txt")
        elif info["pyproject"]:
            install.append("pip install -e . || pip install .")
        install.append("pip install ruff" + (" pytest" if info["python_tests"] else ""))
        steps = f"""      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - name: Install dependencies
        run: |
{_indent(install, 10)}
      - name: Check for errors (syntax, undefined names)
        run: ruff check --select E9,F63,F7,F82 --output-format full .
      - name: Compile every Python file
        run: python -m compileall -q ."""
        if info["python_tests"]:
            steps += """
      - name: Unit tests
        run: pytest -q"""
        jobs.append(f"""  python:
    runs-on: ubuntu-latest
    steps:
{steps}""")
    if info["javascript"]:
        steps = """      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - name: Syntax-check every JavaScript file
        run: |
          status=0
          while IFS= read -r -d '' f; do
            if grep -qE '^\\s*(import|export)\\b' "$f"; then
              out=$(node --input-type=module --check < "$f" 2>&1) || { echo "$out" | sed "s|\\[stdin\\]|$f|"; status=1; }
            else
              node --check "$f" || status=1
            fi
          done < <(find . \\( -name '*.js' -o -name '*.mjs' -o -name '*.cjs' \\) -not -path '*/node_modules/*' -not -path './.git/*' -not -path '*/dist/*' -not -path '*/build/*' -not -name '*.min.js' -print0)
          exit $status"""
        if info["npm_test"]:
            steps += """
      - name: Install and test
        run: |
          npm ci || npm install
          npm test"""
        jobs.append(f"""  javascript:
    runs-on: ubuntu-latest
    steps:
{steps}""")
    if not jobs:
        raise ValueError("No Python or JavaScript files found, so there is nothing for CI to check yet.")
    return (
        "name: Lazarus CI\n\n"
        "# Added by Lazarus so it can see failures. Runs on every branch, including Lazarus fix branches.\n"
        "on: [push, pull_request]\n\n"
        "jobs:\n" + "\n\n".join(jobs) + "\n"
    )


def _indent(lines: list[str], n: int) -> str:
    return "\n".join(" " * n + line for line in lines)


async def ci_status(gh: GitHub, repo: str) -> dict:
    workflows = await gh.list_workflows(repo)
    active = [w for w in workflows if w.get("state") == "active"]
    disabled = [w for w in workflows if str(w.get("state", "")).startswith("disabled")]
    return {
        "repo": repo,
        "disabled": [w.get("name") or w.get("path") for w in disabled],
        "has_ci": bool(active),
        "workflows": [w.get("name") or w.get("path") for w in active],
        "lazarus_ci": any(w.get("path") == WORKFLOW_PATH for w in workflows),
    }


class CISetupError(Exception):
    """A setup problem the user can act on (empty repo, no access...)."""


def manual_url(repo: str, branch: str, yaml: str) -> str:
    """GitHub's 'create new file' page with the workflow already filled in.

    Works with the person's own GitHub login, so it never depends on the token's permissions.
    """
    return (f"https://github.com/{repo}/new/{quote(branch, safe='')}"
            f"?filename={quote(WORKFLOW_PATH, safe='')}&value={quote(yaml, safe='')}")


async def propose_ci(gh: GitHub, repo: str, branch_prefix: str = "lazarus/") -> dict:
    """Open a PR that adds a CI workflow suited to the repo.

    1. Git data API (one commit on a new branch) -> PR
    2. if GitHub refuses: Contents API on a new branch -> PR
    3. if GitHub still refuses (token lacks Workflows permission): return a prefilled
       GitHub editor link so the person can commit the file themselves in one click.
    """
    # -- read the repo; each step reports its own problem
    try:
        meta = await gh.get_repo(repo)
    except GitHubError as e:
        raise CISetupError(f"Lazarus can't read {repo} with this token ({e.status}). "
                           "Check the token's repository access, then press Refresh in Repositories.")
    base = meta.get("default_branch") or "main"
    if meta.get("size") == 0:
        raise CISetupError(f"{repo} is empty. Push some code first, then set up CI.")
    try:
        head_sha = await gh.branch_head(repo, base)
        tree = await gh.get_tree(repo, head_sha)
    except GitHubError as e:
        if e.status in (404, 409):
            raise CISetupError(f"{repo} has no commits on `{base}` yet. Push some code first, then set up CI.")
        raise CISetupError(f"Lazarus couldn't read the files of {repo} ({e.status}).")
    paths = [t["path"] for t in tree]
    pkg = ""
    if "package.json" in paths:
        try:
            pkg = await gh.get_file(repo, "package.json", head_sha)
        except Exception:
            pkg = ""
    info = detect(paths, pkg)
    yaml = build_workflow(info)  # ValueError when there is nothing to check
    checks = []
    if info["python"]:
        checks.append("Python: errors and undefined names" + (", unit tests" if info["python_tests"] else ""))
    if info["javascript"]:
        checks.append("JavaScript: syntax of every file" + (", npm test" if info["npm_test"] else ""))
    result = {"repo": repo, "checks": checks, "workflow": yaml, "pr_url": "", "pr_number": None,
              "manual_url": manual_url(repo, base, yaml), "reason": ""}

    branch = f"{branch_prefix}setup-ci-{int(time.time())}"
    message = "Add CI so Lazarus can see failures"
    errors: list[GitHubError] = []

    # -- route 1: one commit through the Git data API
    created = False
    try:
        sha = await gh.commit_files(repo, head_sha, {WORKFLOW_PATH: (yaml, "100644")}, message)
        await gh.create_branch(repo, branch, sha)
        created = True
    except GitHubError as e:
        errors.append(e)

    # -- route 2: Contents API on a fresh branch
    if not created:
        try:
            await gh.create_branch(repo, branch, head_sha)
            await gh.put_contents(repo, WORKFLOW_PATH, yaml, None, message, branch=branch)
            created = True
        except GitHubError as e:
            errors.append(e)
            try:
                await gh.delete_branch(repo, branch)
            except Exception:
                pass

    if not created:
        statuses = {e.status for e in errors}
        result["reason"] = (
            "GitHub didn't let the token add a workflow file"
            + (" (it needs Workflows: Read and write)" if statuses & {403, 404, 422} else "")
            + f". Status: {', '.join(str(x) for x in sorted(statuses))}."
        )
        return result

    body = (
        "## Lazarus: add CI\n\n"
        "Lazarus fixes failed CI runs, but this repository has no CI checking its code, so bugs stay invisible.\n\n"
        "This workflow checks:\n" + "\n".join(f"- {c}" for c in checks) + "\n\n"
        "It only fails on real errors (not style), and runs on every branch so Lazarus can test its fixes.\n\n"
        f"```yaml\n{yaml}```\n\n> Merge this, then push a change: Lazarus will watch CI from then on."
    )
    try:
        pr = await gh.create_pr(repo, branch, base, "Add CI so Lazarus can catch failures", body)
    except GitHubError as e:
        # branch exists with the file: still useful, point at it
        result["reason"] = f"The workflow was pushed to `{branch}` but GitHub refused the pull request ({e.status})."
        result["manual_url"] = f"https://github.com/{repo}/compare/{base}...{quote(branch, safe='')}?expand=1"
        return result
    result["pr_url"] = pr.get("html_url", "")
    result["pr_number"] = pr.get("number")
    return result
