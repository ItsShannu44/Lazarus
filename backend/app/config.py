"""Runtime configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # GitHub
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    webhook_secret: str = field(default_factory=lambda: os.getenv("GITHUB_WEBHOOK_SECRET", ""))
    allowed_repos: list[str] = field(default_factory=lambda: _list("ALLOWED_REPOS"))  # empty = any

    # Primary LLM: Gemini
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))

    # Fallback LLM: any OpenAI-compatible endpoint (Groq by default, OpenRouter works too)
    fallback_api_key: str = field(default_factory=lambda: os.getenv("FALLBACK_API_KEY", ""))
    fallback_base_url: str = field(
        default_factory=lambda: os.getenv("FALLBACK_BASE_URL", "https://api.groq.com/openai/v1")
    )
    fallback_model: str = field(
        default_factory=lambda: os.getenv("FALLBACK_MODEL", "openai/gpt-oss-120b")
    )

    # Memory (optional). Without it, runs live in RAM.
    mongodb_uri: str = field(default_factory=lambda: os.getenv("MONGODB_URI", ""))
    mongodb_db: str = field(default_factory=lambda: os.getenv("MONGODB_DB", "lazarus"))

    # Agent behaviour
    max_retries: int = field(default_factory=lambda: _int("MAX_RETRIES", 2))  # extra CI attempts after the first
    min_confidence: int = field(default_factory=lambda: _int("MIN_CONFIDENCE", 50))
    verify_timeout_s: int = field(default_factory=lambda: _int("VERIFY_TIMEOUT_S", 600))
    poll_interval_s: int = field(default_factory=lambda: _int("POLL_INTERVAL_S", 10))
    max_files: int = field(default_factory=lambda: _int("MAX_FILES", 4))
    minutes_saved_per_fix: int = field(default_factory=lambda: _int("MINUTES_SAVED_PER_FIX", 45))
    branch_prefix: str = field(default_factory=lambda: os.getenv("BRANCH_PREFIX", "lazarus/"))
    merge_method: str = field(default_factory=lambda: os.getenv("MERGE_METHOD", "squash"))

    # Automation
    # Poll ALLOWED_REPOS for new CI failures every N seconds (0 = webhook only).
    # Lets Lazarus wake up by itself without a public webhook URL.
    watch_poll_s: int = field(default_factory=lambda: _int("WATCH_POLL_S", 20))
    # On escalation, open a GitHub issue with the logs and notes (needs Issues: write on the token)
    escalate_issue: bool = field(default_factory=lambda: _bool("ESCALATE_ISSUE", True))
    # Break/Reset buttons for the stage demo (only act on ALLOWED_REPOS)
    demo_controls: bool = field(default_factory=lambda: _bool("DEMO_CONTROLS", True))

    # Robustness
    max_concurrent: int = field(default_factory=lambda: _int("MAX_CONCURRENT", 1))  # pipelines at once
    llm_patience_s: int = field(default_factory=lambda: _int("LLM_PATIENCE_S", 150))  # wait out rate limits
    gemini_cooldown_s: int = field(default_factory=lambda: _int("GEMINI_COOLDOWN_S", 300))  # skip failing Gemini
    rerun_infra: bool = field(default_factory=lambda: _bool("RERUN_INFRA", True))  # re-run flaky/infra failures

    # Self-healing: recoverable problems are retried by Lazarus instead of flagging a human
    auto_heal: bool = field(default_factory=lambda: _bool("AUTO_HEAL", True))
    auto_retries: int = field(default_factory=lambda: _int("AUTO_RETRIES", 3))  # fresh tries after rate limits, CI timeouts, red CI
    no_run_grace_s: int = field(default_factory=lambda: _int("NO_RUN_GRACE_S", 90))  # wait for CI to start on a fix
    heal_delay_s: int = field(default_factory=lambda: _int("HEAL_DELAY_S", 60))  # wait between automatic retries
    # Low confidence: push anyway and let CI decide (CI is the real judge). False = stop and ask.
    push_low_confidence: bool = field(default_factory=lambda: _bool("PUSH_LOW_CONFIDENCE", True))

    # API
    cors_origins: list[str] = field(default_factory=lambda: _list("CORS_ORIGINS") or ["*"])


settings = Settings()
