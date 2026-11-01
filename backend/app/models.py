"""Data shapes for runs and agent steps (also what the dashboard will consume)."""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal[
    "queued",
    "analyzing",
    "locating",
    "fixing",
    "verifying",
    "opening_pr",
    "awaiting_approval",
    "merged",
    "rejected",
    "needs_human",
    "error",
    "rerun",       # infra/flaky failure: CI re-run requested instead of a code change
    "superseded",  # a newer commit already made the build green
    "waiting",     # recoverable problem: Lazarus retries by itself (rate limit, CI timeout, missing write access)
]

AgentName = Literal[
    "Orchestrator", "Log Analyst", "Code Locator", "Fixer", "Verifier", "PR Agent", "Human"
]


def _id() -> str:
    return uuid.uuid4().hex[:10]


class Step(BaseModel):
    id: str = Field(default_factory=_id)
    run_id: str
    agent: AgentName
    status: Literal["running", "done", "failed", "info"] = "info"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class Edit(BaseModel):
    path: str
    search: str
    replace: str


class Attempt(BaseModel):
    number: int
    reasoning: str = ""
    confidence: int = 0
    edits: list[Edit] = Field(default_factory=list)
    diff: str = ""
    branch: str = ""
    commit_sha: str = ""
    ci_url: str = ""
    passed: bool | None = None
    failure_excerpt: str = ""
    tests: dict[str, int] = Field(default_factory=dict)  # {"passed", "failed", "total"}


class Run(BaseModel):
    id: str = Field(default_factory=_id)
    number: int = 0
    repo: str  # "owner/name"
    ci_run_id: int
    ci_run_url: str = ""
    workflow_name: str = ""
    workflow_id: int = 0
    created_ci_at: float = 0.0  # when the failed CI run was created (GitHub time)
    head_sha: str = ""
    head_branch: str = ""
    run_attempt: int = 1
    status: RunStatus = "queued"

    # Log Analyst output
    error_type: str = ""
    summary: str = ""
    failing_test: str = ""
    log_excerpt: str = ""

    # Code Locator output
    files: list[str] = Field(default_factory=list)

    # Fixer / Verifier history
    attempts: list[Attempt] = Field(default_factory=list)
    confidence: int = 0
    diff: str = ""
    reasoning: str = ""
    fix_branch: str = ""

    # PR
    pr_number: int | None = None
    pr_url: str = ""

    escalation_reason: str = ""
    wait_kind: str = ""              # "access" (needs write access) | "retry" (try again later)
    pr_branch: str = ""              # fix branch the open PR points at (PR may be opened early to get CI)
    retry_at: float | None = None    # when Lazarus will try again by itself
    resumes: int = 0                 # automatic retries used so far
    issue_url: str = ""
    tests_before: dict[str, int] = Field(default_factory=dict)
    tests_after: dict[str, int] = Field(default_factory=dict)
    trigger: str = "manual"  # webhook | watch | manual
    minutes_saved: int = 0
    steps: list[Step] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    finished_at: float | None = None

    def summary_view(self) -> dict[str, Any]:
        return self.model_dump(exclude={"steps", "log_excerpt", "attempts"})
