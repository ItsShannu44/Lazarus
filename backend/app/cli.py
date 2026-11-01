"""Run Lazarus once from the terminal, no server or webhook needed.

    python -m app.cli owner/repo              # fix the latest failed run
    python -m app.cli owner/repo 1234567890   # fix a specific run id
    python -m app.cli owner/repo --approve    # ...and merge the PR when done
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .config import settings
from .github_client import GitHub
from .llm import LLM
from .orchestrator import Orchestrator
from .store import Store

COLORS = {"running": "\033[33m", "done": "\033[32m", "failed": "\033[31m", "info": "\033[36m"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("run_id", nargs="?", type=int)
    ap.add_argument("--branch")
    ap.add_argument("--approve", action="store_true", help="merge the PR if one is opened")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    gh, llm, store = GitHub(settings.github_token), LLM(settings), Store()
    orch = Orchestrator(settings, gh, llm, store)
    q = store.bus.subscribe()
    run = await orch.start_manual(args.repo, args.run_id, args.branch)
    print(f"\n🧟 Lazarus run #{run.number} on {run.repo} (CI run {run.ci_run_id})\n")

    task = next(iter(orch._tasks))
    while not (task.done() and q.empty()):
        try:
            ev = await asyncio.wait_for(q.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if ev["type"] == "step":
            s = ev["step"]
            print(f"{COLORS.get(s['status'], '')}[{s['agent']:>12}] {s['message']}\033[0m")
            if s["data"].get("diff"):
                print(s["data"]["diff"])

    print(f"\nFinal status: {run.status}")
    if run.pr_url:
        print(f"PR: {run.pr_url}")
    if run.escalation_reason:
        print(f"Reason: {run.escalation_reason}")
    if args.approve and run.status == "awaiting_approval":
        await orch.approve(run)
        print("Merged.")
    await gh.close()
    await llm.close()


if __name__ == "__main__":
    asyncio.run(main())
