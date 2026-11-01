"""Which repositories Lazarus watches, chosen from the dashboard.

Saved to a small JSON file (and MongoDB when configured) so the choice survives restarts.
Each repo remembers *when* it was switched on: only CI failures after that moment are fixed,
so turning a repo on never makes Lazarus dig through old history.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("lazarus.watchlist")

DEFAULT_FILE = Path(__file__).resolve().parent.parent / ".lazarus_watch.json"


class WatchList:
    def __init__(self, seed: list[str] | None = None, path: Path | None = DEFAULT_FILE):
        self.path = path
        self.since: dict[str, float] = {}
        loaded = self._load()
        if not loaded and seed:
            now = time.time()
            self.since = {r: now for r in seed}
            self._save()

    # ------------------------------------------------------------------ queries
    @property
    def repos(self) -> list[str]:
        return sorted(self.since)

    def __contains__(self, repo: str) -> bool:
        return repo in self.since

    def watched_since(self, repo: str) -> float:
        return self.since.get(repo, float("inf"))

    # ------------------------------------------------------------------ changes
    def set(self, repo: str, watch: bool) -> bool:
        """Turn watching on/off. Returns True if anything changed."""
        if watch and repo not in self.since:
            self.since[repo] = time.time()
        elif not watch and repo in self.since:
            del self.since[repo]
        else:
            return False
        self._save()
        return True

    # ------------------------------------------------------------------ persistence
    def _load(self) -> bool:
        if not self.path or not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text())
            self.since = {str(k): float(v) for k, v in data.get("since", {}).items()}
            return True
        except Exception as e:
            log.warning("could not read %s: %s", self.path, e)
            return False

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.write_text(json.dumps({"since": self.since}, indent=2))
        except Exception as e:
            log.warning("could not save %s: %s", self.path, e)
