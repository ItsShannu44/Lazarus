"""Orchestrator: runs the agents in order, owns the retry loop and escalation."""
from __future__ import annotations

import asyncio
import logging

import httpx
import time
from datetime import datetime
from typing import Any

from . import agents
from .github_client import GitHub, GitHubError
from .llm import LLM, LLMError
from .models import Attempt, Run, Step
from .store import Store
from .textutil import PatchError, count_tests
from .watchlist import WatchList

log = logging.getLogger("lazarus.orchestrator")


class Orchestrator:
    def __init__(self, settings, gh: GitHub, llm: LLM, store: Store, watch: WatchList | None = None):
        self.s = settings
        # repos chosen on the dashboard; ALLOWED_REPOS in .env only seeds the first list
        self.watch = watch or WatchList(seed=None, path=None)
        self.gh = gh
        self.llm = llm
        self.store = store
        self._nudges: dict[str, asyncio.Event] = {}  # fix branch -> event
        self._tasks: set[asyncio.Task] = set()
        self.started_at = time.time()
        self._slots = asyncio.Semaphore(max(1, settings.max_concurrent))
        self._busy = 0
        self.watch_status: dict[str, Any] = {"enabled": False, "last_poll": None, "error": ""}

    # ------------------------------------------------------------------ intake
    def on_workflow_run(self, payload: dict) -> dict[str, Any]:
        """Decide what to do with a `workflow_run` webhook. Never blocks."""
        action = payload.get("action")
        wr = payload.get("workflow_run") or {}
        repo = (payload.get("repository") or {}).get("full_name", "")
        branch = wr.get("head_branch") or ""

        # Our own fix branches: this is the Verifier's result, never a new incident.
        if branch.startswith(self.s.branch_prefix):
            ev = self._nudges.get(branch)
            if ev:
                ev.set()
            return {"handled": "verifier_nudge", "branch": branch, "waiting": bool(ev)}

        if action != "completed" or wr.get("conclusion") != "failure":
            return {"ignored": f"action={action} conclusion={wr.get('conclusion')}"}
        if self.watched_repos and repo not in self.watched_repos:
            return {"ignored": f"repo {repo} is not being watched; switch it on in the dashboard"}
        run = self.start(repo, wr, trigger="webhook")
        if run is None:
            return {"ignored": "already handled this CI run or commit"}
        return {"started": run.id, "number": run.number}

    def start(self, repo: str, wr: dict, trigger: str = "manual") -> Run | None:
        key = f"{repo}#{wr['id']}#{wr.get('run_attempt', 1)}"
        if key in self.store.seen_ci_runs:
            return None
        active = {"queued", "analyzing", "locating", "fixing", "verifying", "opening_pr", "waiting"}
        if any(r.repo == repo and r.head_sha == wr.get("head_sha") and r.status in active
               for r in self.store.runs.values()):
            return None  # another workflow for the same commit is already being fixed
        self.store.seen_ci_runs.add(key)
        run = Run(
            number=self.store.next_number(),
            repo=repo,
            ci_run_id=wr["id"],
            ci_run_url=wr.get("html_url", ""),
            workflow_name=wr.get("name", ""),
            workflow_id=int(wr.get("workflow_id") or 0),
            created_ci_at=_ts(wr.get("created_at")),
            head_sha=wr.get("head_sha", ""),
            head_branch=wr.get("head_branch", ""),
            run_attempt=int(wr.get("run_attempt") or 1),
            trigger=trigger,
        )
        self.store.runs[run.id] = run
        task = asyncio.create_task(self.process(run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def start_manual(self, repo: str, ci_run_id: int | None = None, branch: str | None = None) -> Run:
        if ci_run_id:
            wr = await self.gh.get_run(repo, ci_run_id)
        else:
            runs = await (self.gh.runs_for_branch(repo, branch) if branch else self.gh.recent_runs(repo, 30))
            where = f"`{branch}`" if branch else repo
            if not runs:
                raise ValueError(
                    f"{where} has no GitHub Actions runs, so there is no failure to fix. Lazarus wakes up when CI "
                    "goes red: add a workflow that builds or checks the code (see the README), push, and try again."
                )
            latest = newest_completed(runs, self.s.branch_prefix)
            current = [w for w in latest if w.get("conclusion") == "failure"]
            if not current:
                if any(w.get("status") != "completed" for w in runs[:5]):
                    raise ValueError(f"CI is still running on {where}. Wait for it to finish, then try again.")
                raise ValueError(
                    f"CI on {where} is green: the latest run passed, so there is nothing to fix. If the code has a bug, "
                    "CI isn't checking for it yet: add a step that runs, builds or lints that code."
                )
            wr = current[0]
        waiting = next((r for r in self.store.runs.values()
                        if r.repo == repo and r.ci_run_id == wr["id"] and r.status == "waiting"), None)
        if waiting:
            await self.resume(waiting, manual=True)
            return waiting
        self.store.seen_ci_runs.discard(f"{repo}#{wr['id']}#{wr.get('run_attempt', 1)}")  # allow manual re-runs
        run = self.start(repo, wr, trigger="manual")
        if run is None:
            raise ValueError("A fix for this commit is already in progress")
        return run

    # ------------------------------------------------------------------ helpers
    async def step(self, run: Run, agent: str, message: str, status: str = "info", **data) -> None:
        log.info("run #%s [%s] %s", run.number, agent, message)
        await self.store.add_step(run, Step(run_id=run.id, agent=agent, status=status, message=message, data=data))

    async def set_status(self, run: Run, status: str) -> None:
        run.status = status
        await self.store.publish_run(run)

    async def escalate(self, run: Run, reason: str, **data) -> None:
        run.escalation_reason = reason
        run.finished_at = time.time()
        await self.set_status(run, "needs_human")
        await self.step(run, "Orchestrator", f"Escalated to a human: {reason}", "failed", **data)
        if self.s.escalate_issue:
            try:
                issue = await self.gh.create_issue(
                    run.repo, f"Lazarus needs a human: run #{run.number} ({run.error_type or 'CI failure'})",
                    agents.issue_body(run),
                )
                run.issue_url = issue.get("html_url", "")
                await self.step(run, "Orchestrator",
                                f"Flagged a human: issue #{issue.get('number')} with logs and notes",
                                "info", issue_url=run.issue_url)
            except Exception as e:
                hint = " (give the token Issues: Read and write)" if "403" in str(e) else ""
                await self.step(run, "Orchestrator", f"Could not open an issue{hint}", "info")

    # ------------------------------------------------------------------ pipeline
    async def process(self, run: Run) -> Run:
        if self._busy >= self.s.max_concurrent:
            await self.step(run, "Orchestrator",
                            f"Queued: {self._busy} fix(es) already running, waiting for a free slot", "info")
        async with self._slots:
            self._busy += 1
            try:
                return await self._process(run)
            finally:
                self._busy -= 1

    async def _process(self, run: Run) -> Run:
        try:
            await self._pipeline(run)
        except agents.NoWriteAccess as e:
            await self.defer(run, str(e), "access")
        except LLMError as e:
            if "No LLM configured" in str(e):
                await self.escalate(run, str(e))
            else:  # rate limits / provider outage: they pass
                await self.defer(run, str(e), "retry")
        except GitHubError as e:
            msg = str(e).lower()
            if e.status >= 500 or e.status == 429 or "rate limit" in msg:
                await self.defer(run, str(e), "retry")
            elif e.status == 403:
                await self.defer(run, str(e), "access")
            else:
                await self.escalate(run, str(e))
        except (agents.VerifyTimeout, agents.NoCIRun, httpx.HTTPError) as e:
            await self.defer(run, str(e) or e.__class__.__name__, "retry")
        except PatchError as e:
            permanent = "non-UTF-8" in str(e) or "removes test" in str(e) or "deletes a test" in str(e) \
                or "skips a test" in str(e) or "silences" in str(e)
            await (self.escalate(run, str(e)) if permanent else self.defer(run, str(e), "retry"))
        except agents.NoFailedJobs as e:
            await self.escalate(run, str(e))
        except Exception as e:  # never let a background task die silently
            log.exception("run #%s crashed", run.number)
            run.escalation_reason = f"Internal error: {e}"
            run.finished_at = time.time()
            await self.set_status(run, "error")
            await self.step(run, "Orchestrator", f"Internal error: {e}", "failed")
        return run

    async def _pipeline(self, run: Run) -> None:
        if await self._already_handled(run):
            return
        await self.step(
            run, "Orchestrator",
            f"CI failed on {run.repo}@{run.head_branch} ({run.workflow_name}). Waking up the agents.",
            "done", ci_run_url=run.ci_run_url, head_sha=run.head_sha,
        )

        # 1. Log Analyst
        await self.set_status(run, "analyzing")
        await self.step(run, "Log Analyst", "Reading CI logs…", "running")
        analysis = await agents.log_analyst(self.gh, self.llm, run)
        run.error_type = analysis.get("error_type", "other")
        run.summary = analysis.get("summary", "")
        run.failing_test = analysis.get("failing_test", "") or ""
        run.tests_before = count_tests(run.log_excerpt)
        await self.step(
            run, "Log Analyst", f"{run.error_type}: {run.summary}", "done",
            failing_test=run.failing_test, hypothesis=analysis.get("root_cause_hypothesis", ""),
            key_lines=analysis.get("key_lines", [])[:8], tests_before=run.tests_before,
        )

        # Infra / flaky: the code is fine, so don't touch it. Re-run CI once instead.
        if run.error_type == "infra":
            await self._handle_infra(run)
            return

        # 2. Code Locator
        await self.set_status(run, "locating")
        await self.step(run, "Code Locator", "Choosing which files to open…", "running")
        located = await agents.code_locator(self.gh, self.llm, run, analysis, self.s.max_files)
        run.files = list(located.contents)
        if not run.files:
            await self.defer(run, "Could not find any relevant files to change", "retry")
            return
        await self.step(run, "Code Locator", f"Opened {', '.join(run.files)}", "done", reasons=located.reasons)

        memory = self.store.past_fixes(run.repo, run.error_type)
        if memory:
            await self.step(run, "Orchestrator", f"Found {len(memory)} similar past fix(es) in memory", "info")

        # 3-4. Fixer + Verifier loop
        for n in range(1, self.s.max_retries + 2):
            await self.set_status(run, "fixing")
            await self.step(run, "Fixer", f"Writing patch (attempt {n})…", "running")
            proposal = await agents.fixer(self.gh, self.llm, run, analysis, located, run.attempts, memory)
            attempt = Attempt(
                number=n, reasoning=proposal.reasoning, confidence=proposal.confidence,
                edits=proposal.edits, diff=proposal.diff,
            )
            run.attempts.append(attempt)
            run.confidence, run.diff, run.reasoning = attempt.confidence, attempt.diff, attempt.reasoning
            await self.step(
                run, "Fixer", f"Patch ready, {attempt.confidence}% confident", "done",
                diff=attempt.diff, reasoning=attempt.reasoning, attempt=n,
            )

            if attempt.confidence < self.s.min_confidence and self.s.push_low_confidence:
                await self.step(run, "Orchestrator",
                                f"Confidence is only {attempt.confidence}%, so CI gets the final say on this patch",
                                "info")
            elif attempt.confidence < self.s.min_confidence:
                await self.escalate(
                    run, f"Confidence {attempt.confidence}% is below {self.s.min_confidence}%; "
                    "patch proposed but not pushed", diff=attempt.diff,
                )
                return

            await self.set_status(run, "verifying")
            again = f"-r{run.resumes}" if run.resumes else ""
            branch = f"{self.s.branch_prefix}run-{run.number}-{run.id[:6]}{again}-a{n}"
            attempt.branch = run.fix_branch = branch
            nudge = self._nudges[branch] = asyncio.Event()
            try:
                attempt.commit_sha = await agents.push_fix(self.gh, run, located, proposal, branch)
                await self.step(
                    run, "Verifier", f"Pushed {branch}; GitHub Actions running…", "running",
                    branch=branch, commit=attempt.commit_sha,
                )
                try:
                    passed, ci_runs = await agents.wait_for_ci(
                        self.gh, run.repo, attempt.commit_sha, nudge,
                        self.s.verify_timeout_s, self.s.poll_interval_s, self.s.no_run_grace_s,
                    )
                except agents.NoCIRun:
                    # CI only runs on pull requests (or on main): open the PR now so its CI tests the fix
                    await self.step(run, "Verifier", "CI didn't start on the push, so opening the PR to run its CI",
                                    "running", branch=branch)
                    pr = await self.gh.create_pr(run.repo, branch, run.head_branch, agents.pr_title(run),
                                                 agents.pr_body(run, attempt))
                    run.pr_number, run.pr_url, run.pr_branch = pr.get("number"), pr.get("html_url", ""), branch
                    passed, ci_runs = await agents.wait_for_ci(
                        self.gh, run.repo, attempt.commit_sha, nudge,
                        self.s.verify_timeout_s, self.s.poll_interval_s, self.s.no_run_grace_s,
                    )
            finally:
                self._nudges.pop(branch, None)

            attempt.passed = passed
            attempt.ci_url = ci_runs[0].get("html_url", "") if ci_runs else ""
            if passed:
                attempt.tests = run.tests_after = await agents.ci_test_counts(self.gh, run.repo, ci_runs[0]["id"])
                t = attempt.tests
                extra = f" ({t['passed']}/{t['total']} tests pass)" if t.get("total") else ""
                await self.step(run, "Verifier", f"CI is green on the fix branch ✅{extra}", "done",
                                ci_url=attempt.ci_url, tests=t)
                break

            failed = next((r for r in ci_runs if r.get("conclusion") != "success"), ci_runs[0])
            attempt.failure_excerpt = await agents.fetch_failure_logs(self.gh, run.repo, failed["id"])
            attempt.tests = count_tests(attempt.failure_excerpt)
            await self.step(
                run, "Verifier", f"Tests still red after attempt {n}", "failed",
                ci_url=attempt.ci_url, excerpt=attempt.failure_excerpt[-1500:], tests=attempt.tests,
            )
            await self._cleanup_branch(run, branch)
            if n > self.s.max_retries:
                await self.defer(run, f"CI still failing after {n} attempts", "retry", diff=attempt.diff)
                return
            await self.step(run, "Orchestrator", "Sending the new errors back to the Fixer", "info")

        # Did someone push to the branch meanwhile? If their commit is already green, stand down.
        if await self._superseded(run):
            return

        # 5. PR Agent
        await self.set_status(run, "opening_pr")
        await self.step(run, "PR Agent", "Opening pull request…", "running")
        pr = await agents.pr_agent(self.gh, run, run.attempts[-1])
        run.pr_number, run.pr_url = pr.get("number"), pr.get("html_url", "")
        run.minutes_saved = self.s.minutes_saved_per_fix
        run.finished_at = time.time()
        await self.set_status(run, "awaiting_approval")
        await self.step(
            run, "PR Agent", f"PR #{run.pr_number} open, waiting for human approval", "done",
            pr_url=run.pr_url, seconds_taken=round(run.finished_at - run.created_at),
        )

    # ------------------------------------------------------------------ self-healing
    async def defer(self, run: Run, reason: str, kind: str, **data) -> None:
        """A recoverable problem: park the run and let Lazarus retry it by itself.

        kind="access": the token can't write yet. Retried as soon as write access appears (no limit).
        kind="retry":  rate limit, CI timeout, GitHub outage, red CI. Up to AUTO_RETRIES fresh tries.
        Anything that can't be recovered goes to a human as before.
        """
        if not self.s.auto_heal or (kind == "retry" and run.resumes >= self.s.auto_retries):
            if kind == "retry" and self.s.auto_heal:
                reason = f"{reason} (also after {run.resumes} automatic retries)"
            await self.escalate(run, reason, **data)
            return
        if run.fix_branch:
            await self._cleanup_branch(run, run.fix_branch)
        delay = self.s.heal_delay_s * (1 if kind == "access" else 2 ** run.resumes)
        run.wait_kind, run.retry_at, run.escalation_reason = kind, time.time() + delay, reason
        await self.set_status(run, "waiting")
        if kind == "access":
            msg = (f"Waiting for write access: {reason} Lazarus checks every {self.s.heal_delay_s}s and "
                   "carries on by itself as soon as the token can push.")
        else:
            msg = (f"{reason}. Recoverable, so Lazarus will try again by itself in {delay}s "
                   f"(automatic retry {run.resumes + 1} of {self.s.auto_retries}).")
        await self.step(run, "Orchestrator", msg, "info", **data)

    async def resume(self, run: Run, manual: bool = False) -> Run:
        """Start a parked (or flagged) run again from the top, with a fresh set of attempts."""
        if run.status not in ("waiting", "needs_human", "error"):
            raise ValueError(f"Run is '{run.status}'; only stopped runs can be retried")
        if run.fix_branch:
            await self._cleanup_branch(run, run.fix_branch)
        run.resumes += 1  # also keeps fix-branch names unique
        run.attempts, run.diff, run.reasoning, run.confidence, run.fix_branch = [], "", "", 0, ""
        run.wait_kind, run.retry_at, run.escalation_reason, run.finished_at = "", None, "", None
        run.pr_number, run.pr_url, run.pr_branch = None, "", ""
        await self.set_status(run, "queued")
        await self.step(run, "Orchestrator",
                        "Retrying now (you asked)" if manual else f"Retrying by itself (automatic retry {run.resumes})",
                        "info")
        task = asyncio.create_task(self.process(run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def heal_once(self) -> list[Run]:
        """Wake parked runs whose time has come."""
        resumed: list[Run] = []
        now = time.time()
        for run in list(self.store.runs.values()):
            if run.status != "waiting" or (run.retry_at or 0) > now:
                continue
            newer = [r for r in self.store.runs.values() if r.repo == run.repo and r.head_branch == run.head_branch
                     and r.created_ci_at > run.created_ci_at and r.id != run.id]
            if newer:
                await self._stand_down(run, f"a newer failure on `{run.head_branch}` replaced it (run #{newer[0].number})")
                continue
            if run.wait_kind == "access":
                ok = await self.gh.can_write(run.repo)
                if ok is False:
                    run.retry_at = now + self.s.heal_delay_s  # still read-only; check again later
                    continue
                await self.step(run, "Orchestrator", f"Write access to {run.repo} is there now", "done")
            resumed.append(await self.resume(run))
        return resumed

    async def _handle_infra(self, run: Run) -> None:
        if run.run_attempt > 1:
            await self.escalate(run, "CI failed again after a re-run for a non-code reason "
                                     "(network, runner or flaky test). A human should look at the CI setup.")
            return
        if not self.s.rerun_infra:
            await self.escalate(run, "Failure looks like infrastructure or a flaky test, not the code. Re-run CI.")
            return
        try:
            await self.gh.rerun_failed_jobs(run.repo, run.ci_run_id)
        except GitHubError as e:
            hint = " (give the token Actions: Read and write)" if e.status == 403 else ""
            await self.escalate(run, f"Looks like an infra/flaky failure, but re-running CI failed{hint}: {e}")
            return
        run.finished_at = time.time()
        await self.set_status(run, "rerun")
        await self.step(run, "Orchestrator",
                        "Not a code problem (infra or flaky). Re-ran the failed CI jobs instead of changing code. "
                        "If it fails again, a human gets flagged.", "done")

    async def _superseded(self, run: Run) -> bool:
        try:
            head = await self.gh.branch_head(run.repo, run.head_branch)
        except GitHubError as e:
            if e.status == 404:
                await self._stand_down(run, f"Branch `{run.head_branch}` was deleted while Lazarus was working")
                return True
            return False
        except Exception:
            return False  # can't tell: carry on and open the PR
        if not head or head == run.head_sha:
            return False
        try:
            runs = await self.gh.runs_for_sha(run.repo, head)
        except Exception:
            runs = []
        if runs and all(r.get("status") == "completed" and r.get("conclusion") == "success" for r in runs):
            await self._stand_down(run, f"A newer commit ({head[:7]}) on `{run.head_branch}` already made CI green")
            return True
        await self.step(run, "Orchestrator",
                        f"`{run.head_branch}` moved on to {head[:7]} since the failure; the PR may need a rebase",
                        "info")
        return False

    async def _already_handled(self, run: Run) -> bool:
        """Stop before spending anything if this failure is already fixed or already has a Lazarus PR."""
        try:
            for pr in await self.gh.open_prs(run.repo):
                ref = ((pr.get("head") or {}).get("ref") or "")
                if ref.startswith(self.s.branch_prefix) and f"/actions/runs/{run.ci_run_id}" in (pr.get("body") or ""):
                    await self._stand_down(run, f"a Lazarus PR for this failure is already open (#{pr.get('number')})")
                    return True
        except Exception:
            pass
        try:
            later = [
                w for w in newest_completed(await self.gh.runs_for_branch(run.repo, run.head_branch),
                                            self.s.branch_prefix)
                if _same_workflow(w, run) and _order(w) > (_ts_run(run), run.ci_run_id)
            ]
        except Exception:
            return False
        if later and later[0].get("conclusion") == "success":
            await self._stand_down(
                run, f"already fixed: a newer CI run on `{run.head_branch}` passed ({later[0].get('head_sha', '')[:7]})"
            )
            return True
        return False

    async def _stand_down(self, run: Run, reason: str) -> None:
        if run.fix_branch:
            await self._cleanup_branch(run, run.fix_branch)
        run.escalation_reason = reason
        run.finished_at = time.time()
        await self.set_status(run, "superseded")
        await self.step(run, "Orchestrator", f"Standing down: {reason}. No PR needed.", "done")

    async def _cleanup_branch(self, run: Run, branch: str) -> None:
        if run.pr_number and run.pr_branch == branch:  # an early PR on a failed attempt: close it too
            try:
                await self.gh.close_pr(run.repo, run.pr_number)
            except Exception:
                pass
            run.pr_number, run.pr_url, run.pr_branch = None, "", ""
        try:
            await self.gh.delete_branch(run.repo, branch)
        except Exception as e:
            log.warning("could not delete %s: %s", branch, e)

    @property
    def watched_repos(self) -> list[str]:
        return self.watch.repos or list(self.s.allowed_repos)

    # ------------------------------------------------------------------ watch mode
    async def watch_forever(self) -> None:
        """Poll ALLOWED_REPOS for new CI failures and for PRs merged/closed on GitHub.

        Makes Lazarus wake up by itself even without a public webhook URL.
        Only failures that finish after the server started are picked up.
        """
        if not self.s.watch_poll_s:
            log.info("watch mode off (WATCH_POLL_S=0)")
            return
        self.watch_status["enabled"] = True
        log.info("watching %s every %ss", ", ".join(self.watched_repos) or "no repos yet", self.s.watch_poll_s)
        while True:
            try:
                await self.poll_once()
                await self.heal_once()
                self.watch_status.update(last_poll=time.time(), error="")
            except Exception as e:
                self.watch_status["error"] = str(e)[:200]
                log.warning("watch poll failed: %s", e)
            await asyncio.sleep(self.s.watch_poll_s)

    async def poll_once(self) -> list[Run]:
        started: list[Run] = []
        for repo in self.watched_repos:
            # only failures after Lazarus started AND after this repo was switched on
            since = max(self.started_at, self.watch.watched_since(repo)) if repo in self.watch else self.started_at
            try:
                recent = await self.gh.recent_runs(repo, 30)
            except GitHubError as e:
                log.warning("cannot read CI runs of %s: %s", repo, e)
                continue
            for wr in newest_completed(recent, self.s.branch_prefix):
                if wr.get("conclusion") != "failure":
                    continue  # the latest result for this branch+workflow is green: nothing to do
                if _ts(wr.get("updated_at")) < since:
                    continue
                run = self.start(repo, wr, trigger="watch")
                if run:
                    started.append(run)
        await self.sync_prs()
        return started

    async def sync_prs(self) -> None:
        """A PR merged or closed directly on GitHub counts as the human's decision."""
        for run in list(self.store.runs.values()):
            if run.status != "awaiting_approval" or not run.pr_number:
                continue
            try:
                pr = await self.gh.get_pr(run.repo, run.pr_number)
            except Exception:
                continue
            if pr.get("merged"):
                await self.set_status(run, "merged")
                await self.step(run, "Human", f"PR #{run.pr_number} merged on GitHub 🧟➡️🧑", "done")
            elif pr.get("state") == "closed":
                run.minutes_saved = 0
                await self.set_status(run, "rejected")
                await self.step(run, "Human", f"PR #{run.pr_number} closed on GitHub", "failed")

    # ------------------------------------------------------------------ human decisions
    async def approve(self, run: Run) -> Run:
        if run.status != "awaiting_approval" or not run.pr_number:
            raise ValueError(f"Run is '{run.status}', nothing to approve")
        try:
            await self.gh.merge_pr(run.repo, run.pr_number, self.s.merge_method)
        except GitHubError as e:
            if e.status in (405, 409, 422):
                raise ValueError(
                    f"GitHub can't merge PR #{run.pr_number} automatically (merge conflict, branch protection "
                    f"or required checks). Open it on GitHub to resolve: {run.pr_url}"
                )
            raise
        await self._cleanup_branch(run, run.fix_branch)
        await self.set_status(run, "merged")
        await self.step(run, "Human", f"Approved and merged PR #{run.pr_number} 🧟➡️🧑", "done")
        return run

    async def reject(self, run: Run, reason: str = "") -> Run:
        if run.status in ("merged", "rejected"):
            raise ValueError(f"Run is already '{run.status}'")
        if run.status not in ("awaiting_approval", "needs_human", "error"):
            raise ValueError(f"Run is still '{run.status}'; wait for it to finish")
        if run.pr_number:
            try:
                await self.gh.close_pr(run.repo, run.pr_number)
            except GitHubError as e:
                log.warning("close PR failed: %s", e)
        if run.fix_branch:
            await self._cleanup_branch(run, run.fix_branch)
        run.minutes_saved = 0
        await self.set_status(run, "rejected")
        await self.step(run, "Human", f"Rejected{': ' + reason if reason else ''}", "failed")
        return run


def _order(wr: dict) -> tuple[float, int]:
    return (_ts(wr.get("created_at")), int(wr.get("id") or 0))


def _ts_run(run: Run) -> float:
    return run.created_ci_at or 0.0


def _same_workflow(wr: dict, run: Run) -> bool:
    if run.workflow_id and wr.get("workflow_id"):
        return int(wr["workflow_id"]) == run.workflow_id
    return (wr.get("name") or "") == run.workflow_name


def newest_completed(runs: list[dict], fix_prefix: str) -> list[dict]:
    """The latest finished CI result for each (branch, workflow), newest first.

    A failure only needs fixing if it is still the latest result: once a newer run on the
    same branch and workflow passes, that failure is history, not work.
    """
    best: dict[tuple, dict] = {}
    for wr in runs:
        if wr.get("status") != "completed" or (wr.get("head_branch") or "").startswith(fix_prefix):
            continue
        key = (wr.get("head_branch"), wr.get("workflow_id") or wr.get("name"))
        if key not in best or _order(wr) > _order(best[key]):
            best[key] = wr
    return sorted(best.values(), key=_order, reverse=True)


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
