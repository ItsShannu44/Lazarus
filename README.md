# Lazarus 🧟 — raises failed CI builds from the dead

When a GitHub Actions build fails, Lazarus reads the logs, finds the root cause, writes a fix,
pushes it to a `lazarus/*` branch so GitHub Actions re-runs the tests, retries up to 2 times on
red, and opens a PR. **Nothing merges until a human approves.**

```
CI fails ─▶ webhook or watch mode ─▶ Orchestrator
                        ├─ Log Analyst   (GitHub jobs + logs API, Gemini)
                        ├─ Code Locator  (git tree + contents API, Gemini)
                        ├─ Fixer         (Gemini → JSON search/replace patch)
                        ├─ Verifier      (1 commit via git data API → branch → wait for Actions)
                        │     └─ red? errors go back to the Fixer (max 2 retries) → else escalate
                        ├─ PR Agent      (pulls API, includes tests X/Y) ─▶ Human: approve (merge) / reject
                        └─ escalation    ─▶ GitHub issue with CI logs, attempts and agent notes
Every step → MongoDB (optional) + live SSE feed for the dashboard
```

## Layout
```
backend/app/
  main.py          FastAPI: webhook, runs API, approve/reject, SSE
  orchestrator.py  pipeline, retry loop, escalation, human actions
  agents.py        the 5 agents + prompts + JSON schemas
  github_client.py GitHub REST calls
  llm.py           Gemini (structured output) + OpenAI-compatible fallback
  store.py         runs in RAM (+ Mongo write-through), event bus
  textutil.py      log cleaning, patch apply, diff
  cli.py           run once from the terminal, no server needed
  check.py         pre-flight: tests GitHub token, Gemini, Groq
  demo.py          Break/Reset buttons for the stage demo
backend/tests/     offline end-to-end tests (fake GitHub + fake LLM)
dashboard/         Next.js + Tailwind UI (see dashboard/README.md)
demo-app/          the repo you push to GitHub and break on stage
scripts/break_demo.py   test | dep | lint | reset
```

## Setup (≈15 min)

1. **Demo repo.** Create an empty GitHub repo (e.g. `demo-app`), then:
   ```bash
   cd demo-app && git init && git add -A && git commit -m init
   git branch -M main && git remote add origin git@github.com:<you>/demo-app.git && git push -u origin main
   ```
   Check the first CI run is green. The workflow runs on `push` to every branch, which Lazarus needs.

2. **Token.** Fine-grained PAT scoped to the demo repo: *Actions: read, Contents: read/write,
   Pull requests: read/write, Issues: read/write* (Issues is for escalations; optional).
   Lazarus never edits workflow files, so no Workflows permission.

3. **Backend.**
   ```bash
   cd backend
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements-dev.txt
   cp .env.example .env      # fill GITHUB_TOKEN, GEMINI_API_KEY, ALLOWED_REPOS
   pytest -q                 # 76 offline tests, no keys needed
   uvicorn app.main:app --reload --port 8000
   ```

4. **Waking up.** With `ALLOWED_REPOS` set, **watch mode** checks those repos every
   `WATCH_POLL_S` seconds (default 20), so Lazarus starts by itself with no webhook.
   It also notices PRs you merge or close directly on GitHub.

   **Webhook (optional, instant).** For local dev use smee.io (no account needed):
   ```bash
   npx smee-client --url https://smee.io/<your-channel> --target http://localhost:8000/webhook/github
   ```
   Repo → Settings → Webhooks → Payload URL = the smee URL (or `https://<render-app>/webhook/github`),
   content type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`,
   events: **Workflow runs** only.

## Choosing repositories

Open **Repositories** on the dashboard. It lists every repository your GitHub token can reach;
switch **Watch** on for the ones Lazarus should look after. From then on, every failed CI run in a
watched repo wakes Lazarus up automatically (watch mode checks every `WATCH_POLL_S` seconds, or
instantly with a webhook). Only failures *after* you switch a repo on are fixed, never old history.

- The choice is saved in `backend/.lazarus_watch.json`, so it survives restarts.
- `ALLOWED_REPOS` in `.env` only seeds the list the first time.
- Read-only repos are shown but can't be switched on (Lazarus needs to push a fix branch).
- Fine-grained tokens only see the repositories selected on the token: to add more, edit the token
  on GitHub (or pick "All repositories"), then press **Refresh** in the panel.

### Repos without CI
Lazarus wakes up when **GitHub Actions goes red**. A repo with no workflow (or one that never checks
the code) stays green however broken it is, so Lazarus never sees the bug. The dashboard warns
*"No CI in this repo"* and offers **Set up CI**: Lazarus looks at the files and opens a PR adding
`.github/workflows/lazarus-ci.yml`:

- **Python:** installs requirements, `ruff --select E9,F63,F7,F82` (syntax errors, undefined names; never style),
  `compileall`, and `pytest` if the repo has tests.
- **JavaScript:** `node --check` on every file (ES modules checked as modules), `npm test` if defined.

Merge the PR, then push a change. Needs the token permission **Workflows: Read and write**.
Without it, Lazarus opens GitHub's editor with the workflow already filled in: press *Commit changes*
(this uses your own GitHub login, so the token's permissions don't matter).

## Run everything

```powershell
# Terminal 1 – backend
cd backend
python -m app.check <you>/demo-app     # all ✔ ?
uvicorn app.main:app --port 8000

# Terminal 2 – dashboard
cd dashboard
npm install
copy .env.local.example .env.local      # macOS/Linux: cp
npm run dev                             # http://localhost:3000
```

## Try it

```bash
# break the demo (commits + pushes → CI fails → webhook → Lazarus wakes up)
python scripts/break_demo.py test  --repo <you>/demo-app   # or dep / lint (edits GitHub directly)
python scripts/break_demo.py reset --repo <you>/demo-app   # green again

# no webhook? run it by hand on the latest failed run:
cd backend && python -m app.cli <you>/demo-app            # prints the agent timeline
curl -X POST localhost:8000/api/runs/trigger -H 'content-type: application/json' -d '{"repo":"<you>/demo-app"}'
```

## API (for the dashboard)

| Method | Path | What |
|---|---|---|
| POST | `/webhook/github` | GitHub `workflow_run` events (HMAC-verified) |
| POST | `/api/runs/trigger` | `{repo, run_id?, branch?}` start manually |
| GET | `/api/runs` | run list (no steps) |
| GET | `/api/runs/{id}` | full run: steps, attempts, diff, PR |
| POST | `/api/runs/{id}/approve` | merge the PR |
| POST | `/api/runs/{id}/reject` | `{reason?}` close PR + delete branch |
| GET | `/api/stats` | totals + minutes saved |
| GET | `/api/config` | watched repos, watch status, demo buttons |
| GET | `/api/repos` | repositories the token can reach, with `watched` / `can_push` |
| POST | `/api/repos/watch` | `{repo, watch}` switch automatic fixing on or off |
| GET | `/api/repos/ci?repo=` | does the repo have GitHub Actions workflows |
| POST | `/api/repos/ci/setup` | `{repo}` open a PR that adds a suitable CI workflow |
| POST | `/api/demo/break` | `{bug: test\|dep\|lint, repo?}` push a bug to the demo repo |
| POST | `/api/demo/reset` | `{repo?}` make the demo repo green again |
| GET | `/api/stream` | SSE: every step of every run |
| GET | `/api/runs/{id}/stream` | SSE for one run |

SSE messages are JSON: first `{"type":"snapshot",...}`, then
`{"type":"step","run":{...},"step":{agent,status,message,data,ts}}` and `{"type":"run","run":{...}}`.
`step.data` carries `diff`, `reasoning`, `pr_url`, `ci_url`, `branch` where relevant.

Run statuses: `queued → analyzing → locating → fixing → verifying → opening_pr → awaiting_approval → merged | rejected`,
or `needs_human` / `error` with `escalation_reason`.

## Slide checklist

| Slide promise | Where |
|---|---|
| Wakes up when CI fails | webhook + watch mode (`orchestrator.watch_forever`) |
| Log Analyst / Code Locator / Fixer (JSON patch) / Verifier / PR Agent | `agents.py` |
| Up to 2 retries, errors go back to the Fixer | `orchestrator._pipeline` |
| Low confidence or still failing → flag a human with logs and notes | escalation + GitHub issue |
| Nothing merges until a human approves | Approve button → merge API; GitHub merges synced |
| Wireframe: failed runs, timeline, diff, confidence, Tests X/Y, approve/reject, time saved | dashboard |
| MongoDB memory: every run saved, past fixes reused | `store.py` (`MONGODB_URI`) |
| Gemini + Groq/OpenRouter backup | `llm.py` with model auto-discovery |
| Live demo with 3 bugs | Stage demo bar on the dashboard |

## Self-healing (Lazarus finishes the job by itself)

A problem that will go away pauses the run as **Will retry** (orange) instead of flagging a human:

| Problem | What Lazarus does |
|---|---|
| Token can read the repo but not push (public repo not on the token) | Keeps the fix, re-checks write access every `HEAL_DELAY_S`, continues the moment you add the repo to the token |
| LLM rate limit / provider outage | Retries after 60s, 120s, 240s |
| CI on the fix branch stays red after all attempts, or the patch didn't apply | Starts fresh (new attempts), up to `AUTO_RETRIES` |
| CI only runs on `pull_request` (not on push to other branches) | Opens the PR early so its CI verifies the fix |
| GitHub 5xx / network error / CI timeout | Retries |
| Low confidence | Pushes anyway; CI is the judge and a human still approves the merge |

Only truly unrecoverable cases reach **Needs a human**: binary files, attempts to cheat (skipping or deleting tests), a broken workflow file, flaky infra twice, or retries used up. Every stopped run has a **Retry now** button. The dashboard warns when the token can't push to the selected repo.

## Edge cases (each has a test in `backend/tests/test_edge_cases.py`)

| Situation | What Lazarus does |
|---|---|
| Fix still fails CI | New errors go back to the Fixer, max 2 retries, then a human is flagged (issue with logs) |
| Fixer isn't sure | Below `MIN_CONFIDENCE` the patch is shown but never pushed |
| Patch doesn't match the code | Fixer rewrites it without spending a CI run |
| Fixer tries to cheat (`noqa`, `type: ignore`, skip/xfail, deleting tests or asserts, `|| true`) | Patch rejected, Fixer told to fix the real cause |
| Edits to `.github/` workflows | Blocked |
| Patch too big (>200 lines) | Rejected, then a human is flagged |
| Big source file (up to 400 KB) | Only ~120 lines around the error line from the log go to the LLM; the edit still applies to the whole file |
| Workflow exists but Actions is off | Dashboard says so and links to the repo's Actions settings |
| Binary / non-UTF-8 file | Never rewritten |
| Secrets in CI logs | Redacted before reaching the LLM, PRs or issues |
| Prompt injection in logs/code | Logs are treated as data; the agent can only propose edits, never merge |
| Flaky test / network / runner outage | Classified as `infra`: re-runs CI once instead of changing code; fails again → human |
| Workflow file itself invalid (no jobs ran) | Human flagged immediately, no LLM spend |
| Someone pushes a fix meanwhile | If the newer commit is green, Lazarus stands down (`superseded`); otherwise notes the PR may need a rebase |
| Branch deleted mid-run | Stands down |
| Failure on Lazarus's own `lazarus/*` branch | Treated as a test result, never a new incident (no loops) |
| Same failure delivered twice | Deduplicated; a CI *re-run* that fails again is a new incident |
| Old failure that is already fixed (newer run on that branch passed) | Never re-fixed, by watch mode, webhook or the Run button; stands down before any LLM call |
| Lazarus PR for this failure already open (e.g. after a restart) | Stands down instead of opening a duplicate |
| Two workflows fail on one commit | Fixed once |
| Many failures at once | At most `MAX_CONCURRENT` pipelines; the rest queue |
| Server restarts mid-run | Interrupted runs are marked `error` on startup (with MongoDB) |
| Merge conflict / branch protection on Approve | Clear message + link to resolve on GitHub; run stays approvable |
| Token expired / missing permission | Plain-English hint naming the missing permission |
| Token can read a public repo but not write it | Tries a second write API; if both refuse, says the repo must be added to the token's Repository access (fix stays visible) |
| Gemini overloaded or model retired | Retries, auto-switches model, falls back to Groq |
| Free-tier rate limit (429, tokens per minute) | Waits exactly as long as the provider says, rotates to another Groq model with its own limit, trims prompts; only flags a human after `LLM_PATIENCE_S` |
| CI doesn't run on fix branches | Escalates with the exact trigger to add |
| PR merged/closed on GitHub directly | Dashboard syncs it |

## Safety
- Human approval required for every merge
- Fixes run only inside GitHub Actions; nothing executes on our server
- Patches can't touch `.github/`, can't exceed 200 changed lines, must match the file exactly
- Confidence below `MIN_CONFIDENCE` → patch shown, never pushed
- Failures on `lazarus/*` branches are verifier results, never new incidents (no infinite loops)

## Deploy
`render.yaml` deploys `backend/` on Render (set the env vars in the dashboard). Free instances
sleep: hit `/health` a minute before judging.
