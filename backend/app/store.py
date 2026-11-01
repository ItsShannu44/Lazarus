"""Run storage + live event bus.

In-memory by default; if MONGODB_URI is set, every run is also written through
to MongoDB Atlas so history survives restarts and agents can learn from past fixes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .models import Run, Step

ACTIVE_STATUSES = {"queued", "analyzing", "locating", "fixing", "verifying", "opening_pr"}

log = logging.getLogger("lazarus.store")


class EventBus:
    """Fan-out of agent steps to SSE subscribers. run_id=None means 'all runs'."""

    def __init__(self) -> None:
        self._subs: dict[str | None, set[asyncio.Queue]] = {}

    def subscribe(self, run_id: str | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue, run_id: str | None = None) -> None:
        self._subs.get(run_id, set()).discard(q)

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        for key in (run_id, None):
            for q in list(self._subs.get(key, ())):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # slow client; drop rather than block the agents


class Store:
    def __init__(self, mongodb_uri: str = "", db_name: str = "lazarus") -> None:
        self.runs: dict[str, Run] = {}
        self.seen_ci_runs: set[str] = set()
        self.bus = EventBus()
        self._counter = 0
        self._col = None
        if mongodb_uri:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient

                self._col = AsyncIOMotorClient(mongodb_uri)[db_name]["runs"]
            except Exception as e:  # pragma: no cover
                log.warning("MongoDB disabled: %s", e)

    async def load(self) -> None:
        """Pull recent history from Mongo on startup."""
        if self._col is None:
            return
        try:
            async for doc in self._col.find().sort("created_at", -1).limit(200):
                doc.pop("_id", None)
                run = Run.model_validate(doc)
                if run.status in ACTIVE_STATUSES:  # server died mid-run
                    run.status = "error"
                    run.escalation_reason = "Interrupted by a server restart. Re-run it from the dashboard."
                    await self.save(run)
                self.runs[run.id] = run
                self._counter = max(self._counter, run.number)
                self.seen_ci_runs.add(f"{run.repo}#{run.ci_run_id}#{run.run_attempt}")
        except Exception as e:
            log.warning("Could not load runs from MongoDB: %s", e)

    def next_number(self) -> int:
        self._counter += 1
        return self._counter

    async def save(self, run: Run) -> None:
        self.runs[run.id] = run
        if self._col is not None:
            try:
                await self._col.replace_one({"id": run.id}, run.model_dump(), upsert=True)
            except Exception as e:
                log.warning("MongoDB write failed: %s", e)

    async def add_step(self, run: Run, step: Step) -> None:
        run.steps.append(step)
        self.bus.publish(
            run.id,
            {"type": "step", "run": run.summary_view(), "step": step.model_dump()},
        )
        await self.save(run)

    async def publish_run(self, run: Run) -> None:
        self.bus.publish(run.id, {"type": "run", "run": run.summary_view()})
        await self.save(run)

    def list_runs(self) -> list[Run]:
        return sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)

    def past_fixes(self, repo: str, error_type: str, limit: int = 3) -> list[Run]:
        """Memory: previous successful fixes for this repo + error type."""
        good = [
            r
            for r in self.list_runs()
            if r.repo == repo
            and r.status in ("awaiting_approval", "merged")
            and r.diff
            and (not error_type or r.error_type == error_type)
        ]
        return good[:limit]

    def stats(self) -> dict[str, Any]:
        runs = list(self.runs.values())
        fixed = [r for r in runs if r.status in ("awaiting_approval", "merged")]
        return {
            "total_runs": len(runs),
            "fixed": len(fixed),
            "merged": sum(r.status == "merged" for r in runs),
            "needs_human": sum(r.status == "needs_human" for r in runs),
            "minutes_saved": sum(r.minutes_saved for r in runs),
        }
