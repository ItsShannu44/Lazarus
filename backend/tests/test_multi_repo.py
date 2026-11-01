"""Pick repositories from everything the token can reach; Lazarus watches only those."""
import asyncio
import time

from app.watchlist import WatchList

from .fakes import failed_run
from .test_pipeline import client_with, make


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def repo(name, push=True, private=False):
    return {"full_name": name, "private": private, "description": f"{name} repo", "default_branch": "main",
            "pushed_at": "2026-10-31T10:00:00Z", "language": "Python", "permissions": {"push": push}}


def fail(run_id, t, owner_repo):
    return failed_run(run_id) | {"updated_at": iso(t), "created_at": iso(t), "workflow_id": 1,
                                 "head_sha": f"s{run_id}", "repository": {"full_name": owner_repo}}


def test_watchlist_persists_and_remembers_when_each_repo_was_switched_on(tmp_path):
    f = tmp_path / "w.json"
    w = WatchList(seed=["me/a"], path=f)
    assert w.repos == ["me/a"]
    before = time.time()
    w.set("me/b", True)
    w.set("me/a", False)
    again = WatchList(seed=["me/zzz"], path=f)  # seed is ignored once a saved list exists
    assert again.repos == ["me/b"] and again.watched_since("me/b") >= before
    assert again.watched_since("me/a") == float("inf")


def test_lists_token_repos_and_toggles_watch():
    orch, gh, _ = make()
    gh.repos = [repo("me/demo-app"), repo("me/api"), repo("org/readonly", push=False, private=True)]
    with client_with(orch) as c:
        listed = {r["full_name"]: r for r in c.get("/api/repos").json()}
        assert set(listed) == {"me/demo-app", "me/api", "org/readonly"}
        assert listed["org/readonly"]["can_push"] is False and listed["org/readonly"]["private"] is True
        assert not any(r["watched"] for r in listed.values())

        assert c.post("/api/repos/watch", json={"repo": "me/api", "watch": True}).json()["repos"] == ["me/api"]
        assert c.post("/api/repos/watch", json={"repo": "ME/DEMO-APP", "watch": True}).json()["repos"] == ["me/api", "me/demo-app"]
        assert c.post("/api/repos/watch", json={"repo": "org/readonly", "watch": True}).status_code == 403
        assert c.post("/api/repos/watch", json={"repo": "someone/else", "watch": True}).status_code == 404
        assert {r["full_name"] for r in c.get("/api/repos").json() if r["watched"]} == {"me/api", "me/demo-app"}
        assert c.get("/api/config").json()["repos"] == ["me/api", "me/demo-app"]

        assert c.post("/api/repos/watch", json={"repo": "me/api", "watch": False}).json()["repos"] == ["me/demo-app"]


def test_watch_mode_fixes_each_selected_repo_and_ignores_the_rest():
    orch, gh, _ = make()
    orch.started_at = 0
    now = time.time()
    orch.watch.since = {"me/api": now - 100, "me/demo-app": now - 100}
    gh.recent_by_repo = {
        "me/api": [fail(1, now - 10, "me/api")],                # after switch-on -> fix
        "me/demo-app": [fail(2, now - 500, "me/demo-app")],     # before switch-on -> history, ignore
        "me/unwatched": [fail(3, now - 5, "me/unwatched")],     # not selected -> never even polled
    }

    async def go():
        started = await orch.poll_once()
        await asyncio.gather(*orch._tasks)
        return started
    started = asyncio.run(go())
    assert [(r.repo, r.ci_run_id) for r in started] == [("me/api", 1)]
    assert started[0].status == "awaiting_approval"
    assert sorted(set(gh.polled)) == ["me/api", "me/demo-app"]


def test_switching_a_repo_on_later_only_catches_new_failures():
    orch, gh, _ = make()
    orch.started_at = 0
    now = time.time()
    gh.recent_by_repo = {"me/api": [fail(7, now - 30, "me/api")]}
    orch.watch.set("me/api", True)  # switched on now: the failure 30s ago is history
    assert asyncio.run(orch.poll_once()) == []


def test_webhook_from_unwatched_repo_is_ignored():
    orch, gh, _ = make()
    orch.watch.set("me/api", True)
    payload = {"action": "completed", "workflow_run": failed_run(9), "repository": {"full_name": "me/other"}}
    assert "not being watched" in orch.on_workflow_run(payload)["ignored"]


def test_demo_buttons_only_touch_watched_repos():
    orch, gh, _ = make()
    orch.s.demo_controls = True
    orch.watch.set("me/demo-app", True)
    with client_with(orch) as c:
        assert c.post("/api/demo/break", json={"bug": "test", "repo": "me/other"}).status_code == 400
        assert c.post("/api/demo/break", json={"bug": "test", "repo": "me/demo-app"}).status_code == 200


def test_stage_demo_on_a_non_demo_repo_explains_instead_of_404():
    from app.github_client import GitHubError
    orch, gh, _ = make()
    orch.s.demo_controls = True
    orch.watch.set("me/demo-app", True)
    orch.watch.set("me/portfolio", True)
    real_get = gh.get_contents

    async def get_contents(repo, path):
        if repo == "me/portfolio":
            raise GitHubError(404, '{"message":"Not Found"}')
        return await real_get(repo, path)
    gh.get_contents = get_contents
    with client_with(orch) as c:
        assert c.get("/api/demo/check", params={"repo": "me/demo-app"}).json()["supported"] is True
        chk = c.get("/api/demo/check", params={"repo": "me/portfolio"}).json()
        assert chk["supported"] is False and "copy of demo-app" in chk["reason"]
        r = c.post("/api/demo/break", json={"bug": "test", "repo": "me/portfolio"})
        assert r.status_code == 409 and "copy of demo-app" in r.json()["detail"]
        assert c.post("/api/demo/reset", json={"repo": "me/portfolio"}).status_code == 409
