"""Repos without CI: Lazarus explains why it can't see failures and proposes a workflow."""
import yaml

from app.ci_setup import WORKFLOW_PATH, build_workflow, detect

from .test_pipeline import client_with, make


def test_detects_python_fastapi_repo():
    info = detect(["main.py", "app/routers/users.py", "requirements.txt"])
    assert info["python"] and info["requirements"] and not info["javascript"] and not info["python_tests"]
    doc = yaml.safe_load(build_workflow(info))
    runs = " ".join(s.get("run", "") for s in doc["jobs"]["python"]["steps"])
    assert "pip install -r requirements.txt" in runs and "ruff check --select E9,F63,F7,F82" in runs
    assert "pytest" not in runs  # no tests in the repo -> no pytest step that would fail on "no tests"
    assert doc[True] == ["push", "pull_request"]  # every branch, so Lazarus can test its fixes


def test_detects_plain_browser_js_repo():
    info = detect(["index.html", "js/app.js", "js/core.js", "node_modules/lib/a.js"])
    assert info["javascript"] and info["js_files"] == 2 and not info["python"] and not info["npm_test"]
    assert "node --check" in build_workflow(info)


def test_npm_test_only_when_real():
    assert detect(["package.json", "a.js"], '{"scripts":{"test":"jest"}}')["npm_test"]
    assert not detect(["package.json", "a.js"], '{"scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}')["npm_test"]


def test_ci_status_and_setup_pr():
    orch, gh, _ = make()
    gh.files = {"main.py": "app = 1\n", "requirements.txt": "fastapi\n"}
    with client_with(orch) as c:
        st = c.get("/api/repos/ci", params={"repo": "me/FastAPI"}).json()
        assert st["has_ci"] is False and st["workflows"] == []
        r = c.post("/api/repos/ci/setup", json={"repo": "me/FastAPI"}).json()
    assert r["pr_url"].endswith("/pull/1") and any("Python" in x for x in r["checks"])
    pr = gh.prs[1]
    assert pr["head"].startswith("lazarus/setup-ci-") and pr["base"] == "main"
    committed = next(iter(gh.commits.values()))["files"]
    assert WORKFLOW_PATH in committed and "ruff check" in committed[WORKFLOW_PATH][0]


def test_existing_ci_is_reported():
    orch, gh, _ = make()
    gh.workflows = [{"name": "Tests", "path": ".github/workflows/test.yml", "state": "active"}]
    with client_with(orch) as c:
        st = c.get("/api/repos/ci", params={"repo": "me/demo-app"}).json()
    assert st["has_ci"] is True and st["workflows"] == ["Tests"]


def test_falls_back_to_contents_api_when_git_data_api_refuses():
    from app.github_client import GitHubError
    orch, gh, _ = make()
    gh.files = {"app.js": "x()\n"}

    async def refused(*a, **k):
        raise GitHubError(404, "Not Found")
    gh.commit_files = refused
    with client_with(orch) as c:
        r = c.post("/api/repos/ci/setup", json={"repo": "me/js-core"}).json()
    assert r["pr_url"].endswith("/pull/1") and gh.contents_branch.startswith("lazarus/setup-ci-")


def test_without_workflow_permission_gives_prefilled_github_editor_link():
    from urllib.parse import parse_qs, urlparse
    from app.github_client import GitHubError
    orch, gh, _ = make()
    gh.files = {"main.py": "app = 1\n", "requirements.txt": "fastapi\n"}
    gh.contents_forbidden = True

    async def refused(*a, **k):
        raise GitHubError(404, "Not Found")
    gh.commit_files = refused
    with client_with(orch) as c:
        res = c.post("/api/repos/ci/setup", json={"repo": "me/FastAPI"})
    assert res.status_code == 200
    r = res.json()
    assert r["pr_url"] == "" and "Workflows: Read and write" in r["reason"]
    u = urlparse(r["manual_url"])
    assert u.netloc == "github.com" and u.path == "/me/FastAPI/new/main"
    q = parse_qs(u.query)
    assert q["filename"] == [WORKFLOW_PATH] and "ruff check" in q["value"][0]
    assert not gh.prs and gh.deleted  # half-made branch cleaned up


def test_empty_repo_is_explained():
    orch, gh, _ = make()

    async def empty(repo):
        return {"full_name": repo, "default_branch": "main", "size": 0}
    gh.get_repo = empty
    with client_with(orch) as c:
        r = c.post("/api/repos/ci/setup", json={"repo": "me/new-repo"})
    assert r.status_code == 409 and "is empty" in r.json()["detail"]


def test_unreadable_repo_is_explained():
    from app.github_client import GitHubError
    orch, gh, _ = make()

    async def nope(repo):
        raise GitHubError(404, "Not Found")
    gh.get_repo = nope
    with client_with(orch) as c:
        r = c.post("/api/repos/ci/setup", json={"repo": "me/secret"})
    assert r.status_code == 409 and "can't read" in r.json()["detail"]


def test_repo_with_nothing_to_check():
    orch, gh, _ = make()
    gh.files = {"README.md": "# hi"}
    with client_with(orch) as c:
        r = c.post("/api/repos/ci/setup", json={"repo": "me/docs"})
    assert r.status_code == 409 and "nothing for CI to check" in r.json()["detail"]


def test_disabled_workflow_is_reported_separately():
    orch, gh, _ = make()
    gh.workflows = [{"name": "CI", "path": ".github/workflows/ci.yml", "state": "disabled_manually"}]
    with client_with(orch) as c:
        st = c.get("/api/repos/ci", params={"repo": "me/Bill_split"}).json()
    assert st["has_ci"] is False and st["disabled"] == ["CI"]
