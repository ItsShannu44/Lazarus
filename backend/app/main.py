"""Lazarus API: GitHub webhook in, live agent feed (SSE) and approve/reject out.

Run:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import ci_setup, demo
from .config import settings
from .github_client import GitHub, GitHubError
from .llm import LLM
from .orchestrator import Orchestrator
from .store import Store
from .watchlist import WatchList

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lazarus")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "orch"):  # tests inject their own
        gh, llm = GitHub(settings.github_token), LLM(settings)
        store = Store(settings.mongodb_uri, settings.mongodb_db)
        await store.load()
        app.state.orch = Orchestrator(settings, gh, llm, store, WatchList(seed=settings.allowed_repos))
        if not settings.github_token:
            log.warning("GITHUB_TOKEN is not set")
        if not (settings.gemini_api_key or settings.fallback_api_key):
            log.warning("No LLM key set (GEMINI_API_KEY / FALLBACK_API_KEY)")
    orch = app.state.orch
    watcher = asyncio.create_task(orch.watch_forever())
    yield
    watcher.cancel()
    for closer in (getattr(orch.gh, "close", None), getattr(orch.llm, "close", None)):
        if closer:
            await closer()


app = FastAPI(title="Lazarus", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def orch(request: Request) -> Orchestrator:
    return request.app.state.orch


def get_run_or_404(request: Request, run_id: str):
    run = orch(request).store.runs.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


# --------------------------------------------------------------------------- health
@app.get("/")
@app.get("/health")
async def health(request: Request):
    return {"ok": True, "service": "lazarus", "stats": orch(request).store.stats()}


@app.get("/api/config")
async def get_config(request: Request):
    o = orch(request)
    return {
        "repos": o.watched_repos,
        "watch": {**o.watch_status, "interval_s": o.s.watch_poll_s},
        "demo_controls": o.s.demo_controls,
        "demo_bugs": [{"id": k, "label": v[4]} for k, v in demo.BUGS.items()],
        "min_confidence": o.s.min_confidence,
        "max_retries": o.s.max_retries,
        "auto_heal": o.s.auto_heal,
    }


# --------------------------------------------------------------------------- webhook
def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@app.post("/webhook/github")
async def github_webhook(request: Request):
    body = await request.body()
    if not verify_signature(settings.webhook_secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "bad signature")
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ok": True, "pong": True}
    if event != "workflow_run":
        return {"ignored": f"event {event}"}
    return orch(request).on_workflow_run(json.loads(body))


# --------------------------------------------------------------------------- runs API
class TriggerIn(BaseModel):
    repo: str
    run_id: int | None = None
    branch: str | None = None


class RejectIn(BaseModel):
    reason: str = ""


@app.post("/api/runs/trigger")
async def trigger(request: Request, body: TriggerIn):
    """Start Lazarus by hand on a failed CI run (latest failure if run_id is omitted)."""
    try:
        run = await orch(request).start_manual(body.repo, body.run_id, body.branch)
    except (ValueError, GitHubError) as e:
        raise HTTPException(400, str(e))
    return run.summary_view()


@app.get("/api/runs")
async def list_runs(request: Request):
    return [r.summary_view() for r in orch(request).store.list_runs()]


@app.get("/api/runs/{run_id}")
async def get_run(request: Request, run_id: str):
    return get_run_or_404(request, run_id).model_dump()


@app.post("/api/runs/{run_id}/approve")
async def approve(request: Request, run_id: str):
    run = get_run_or_404(request, run_id)
    try:
        await orch(request).approve(run)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except GitHubError as e:
        raise HTTPException(502, str(e))
    return run.summary_view()


@app.post("/api/runs/{run_id}/reject")
async def reject(request: Request, run_id: str, body: RejectIn | None = None):
    run = get_run_or_404(request, run_id)
    try:
        await orch(request).reject(run, body.reason if body else "")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return run.summary_view()


@app.post("/api/runs/{run_id}/retry")
async def retry(request: Request, run_id: str):
    """Try a stopped run again right now."""
    run = get_run_or_404(request, run_id)
    try:
        await orch(request).resume(run, manual=True)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return run.summary_view()


@app.get("/api/stats")
async def stats(request: Request):
    return orch(request).store.stats()


# --------------------------------------------------------------------------- live feed (SSE)
async def _sse(request: Request, run_id: str | None):
    store = orch(request).store
    q = store.bus.subscribe(run_id)

    async def gen():
        try:
            if run_id:
                snapshot = {"type": "snapshot", "run": store.runs[run_id].model_dump()}
            else:
                snapshot = {"type": "snapshot", "runs": [r.summary_view() for r in store.list_runs()],
                            "stats": store.stats()}
            yield f"event: snapshot\ndata: {json.dumps(snapshot, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            store.bus.unsubscribe(q, run_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/stream")
async def stream_all(request: Request):
    return await _sse(request, None)


@app.get("/api/runs/{run_id}/stream")
async def stream_run(request: Request, run_id: str):
    get_run_or_404(request, run_id)
    return await _sse(request, run_id)


# --------------------------------------------------------------------------- stage demo
class DemoIn(BaseModel):
    bug: str = ""
    repo: str | None = None


def _demo_repo(request: Request, repo: str | None) -> str:
    o = orch(request)
    if not o.s.demo_controls:
        raise HTTPException(403, "Demo controls are off (DEMO_CONTROLS=false)")
    watched = o.watched_repos
    repo = repo or (watched[0] if watched else "")
    if not repo or (watched and repo not in watched):
        raise HTTPException(400, "Demo controls only work on a repository Lazarus is watching")
    return repo


@app.post("/api/demo/break")
async def demo_break(request: Request, body: DemoIn):
    repo = _demo_repo(request, body.repo)
    try:
        msg = await demo.break_repo(orch(request).gh, repo, body.bug)
    except demo.DemoError as e:
        raise HTTPException(409, str(e))
    except GitHubError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "repo": repo, "bug": body.bug, "commit": msg}


_demo_check: dict[str, bool] = {}


@app.get("/api/demo/check")
async def demo_check(request: Request, repo: str):
    """Can the stage-demo buttons act on this repo? (Only copies of demo-app.)"""
    o = orch(request)
    if not o.s.demo_controls:
        return {"repo": repo, "supported": False, "reason": "Demo controls are off (DEMO_CONTROLS=false)"}
    if repo not in _demo_check:
        _demo_check[repo] = await demo.is_demo_repo(o.gh, repo)
    return {"repo": repo, "supported": _demo_check[repo], "reason": "" if _demo_check[repo] else demo.NOT_DEMO}


@app.post("/api/demo/reset")
async def demo_reset(request: Request, body: DemoIn | None = None):
    repo = _demo_repo(request, body.repo if body else None)
    try:
        changed = await demo.reset_repo(orch(request).gh, repo)
    except demo.DemoError as e:
        raise HTTPException(409, str(e))
    except GitHubError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "repo": repo, "changed": changed}


# --------------------------------------------------------------------------- repositories
_repo_cache: dict[str, object] = {"at": 0.0, "items": []}


async def _accessible_repos(request: Request, refresh: bool = False) -> list[dict]:
    import time as _t
    if refresh or _t.time() - float(_repo_cache["at"]) > 60:
        _repo_cache["items"] = await orch(request).gh.list_repos()
        _repo_cache["at"] = _t.time()
    return _repo_cache["items"]  # type: ignore[return-value]


@app.get("/api/repos")
async def list_repos(request: Request, refresh: bool = False):
    """Repositories the GitHub token can reach, and whether Lazarus watches each one."""
    o = orch(request)
    try:
        items = await _accessible_repos(request, refresh)
    except GitHubError as e:
        raise HTTPException(502, str(e))
    watched = set(o.watched_repos)
    out = [
        {
            "full_name": r["full_name"],
            "private": bool(r.get("private")),
            "description": r.get("description") or "",
            "default_branch": r.get("default_branch") or "main",
            "pushed_at": r.get("pushed_at") or "",
            "language": r.get("language") or "",
            "can_push": bool((r.get("permissions") or {}).get("push", True)),
            "watched": r["full_name"] in watched,
        }
        for r in items
    ]
    known = {r["full_name"] for r in out}
    for name in watched - known:  # watched but no longer visible to the token
        out.append({"full_name": name, "private": False, "description": "", "default_branch": "", "pushed_at": "",
                    "language": "", "can_push": False, "watched": True, "missing": True})
    return out


class WatchIn(BaseModel):
    repo: str
    watch: bool = True


@app.post("/api/repos/watch")
async def set_watch(request: Request, body: WatchIn):
    """Switch automatic fixing on or off for one repository."""
    o = orch(request)
    if body.watch:
        try:
            items = await _accessible_repos(request)
        except GitHubError as e:
            raise HTTPException(502, str(e))
        match = next((r for r in items if r["full_name"].lower() == body.repo.lower()), None)
        if not match:
            raise HTTPException(404, f"The token can't see {body.repo}. Add it to the token's repository access.")
        if not (match.get("permissions") or {}).get("push", True):
            raise HTTPException(403, f"The token can only read {body.repo}; Lazarus needs write access to fix it.")
        repo = match["full_name"]
    else:
        repo = next((r for r in o.watch.repos if r.lower() == body.repo.lower()), body.repo)
    o.watch.set(repo, body.watch)
    _write_check.pop(repo, None)  # token may have just been changed
    log.info("%s %s", "watching" if body.watch else "stopped watching", repo)
    return {"repos": o.watched_repos}


# --------------------------------------------------------------------------- CI setup
@app.get("/api/repos/ci")
async def repo_ci(request: Request, repo: str):
    """Does this repo have CI that Lazarus can watch?"""
    gh = orch(request).gh
    try:
        status = await ci_setup.ci_status(gh, repo)
    except GitHubError as e:
        raise HTTPException(502, str(e))
    import time as _t
    hit = _write_check.get(repo)
    if not hit or _t.time() - hit[1] > 30:  # re-checked every 30s, so a token change shows up quickly
        hit = _write_check[repo] = (await gh.can_write(repo) if hasattr(gh, "can_write") else None, _t.time())
    return {**status, "can_write": hit[0]}


_write_check: dict[str, tuple] = {}


class CISetupIn(BaseModel):
    repo: str


@app.post("/api/repos/ci/setup")
async def setup_ci(request: Request, body: CISetupIn):
    """Open a PR that adds a CI workflow suited to the repo (Python and/or JavaScript)."""
    o = orch(request)
    try:
        return await ci_setup.propose_ci(o.gh, body.repo, o.s.branch_prefix)
    except (ValueError, ci_setup.CISetupError) as e:
        raise HTTPException(409, str(e))
    except GitHubError as e:
        raise HTTPException(502, str(e))
