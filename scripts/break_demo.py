#!/usr/bin/env python3
"""Break (or reset) the demo repo so Lazarus has something to revive.

Straight on GitHub (no local clone needed, uses GITHUB_TOKEN from backend/.env):
    python scripts/break_demo.py test  --repo your-user/demo-app
    python scripts/break_demo.py dep   --repo your-user/demo-app
    python scripts/break_demo.py lint  --repo your-user/demo-app
    python scripts/break_demo.py reset --repo your-user/demo-app

Or on a local clone (commits + pushes):
    python scripts/break_demo.py test --repo-dir ../demo-app

Either way CI fails on GitHub, which fires the webhook.
"""
import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

GOOD = {
    "src/shop/cart.py": "\"\"\"A tiny shopping cart used to demo Lazarus.\"\"\"\nfrom __future__ import annotations\n\nfrom dataclasses import dataclass, field\n\n\n@dataclass\nclass Item:\n    name: str\n    price: float\n    qty: int = 1\n\n\ndef check(n: int) -> bool:\n    \"\"\"Quantities may be zero (item kept in cart) but never negative.\"\"\"\n    if n >= 0:\n        return True\n    return False\n\n\n@dataclass\nclass Cart:\n    items: list[Item] = field(default_factory=list)\n\n    def add(self, name: str, price: float, qty: int = 1) -> None:\n        if not check(qty):\n            raise ValueError(\"quantity cannot be negative\")\n        self.items.append(Item(name, price, qty))\n\n    def total(self) -> float:\n        return round(sum(i.price * i.qty for i in self.items), 2)\n\n    def apply_discount(self, percent: float) -> float:\n        if not 0 <= percent <= 100:\n            raise ValueError(\"discount must be between 0 and 100\")\n        return round(self.total() * (1 - percent / 100), 2)\n",
    "src/shop/pricing.py": "\"\"\"Price helpers.\"\"\"\nfrom __future__ import annotations\n\nTAX_RATE = 0.18  # GST\n\n\ndef with_tax(amount: float) -> float:\n    return round(amount * (1 + TAX_RATE), 2)\n\n\ndef format_inr(amount: float) -> str:\n    return f\"₹{amount:,.2f}\"\n",
    "requirements.txt": "pytest==8.3.3\nruff==0.6.9\nrequests==2.32.3\n"
}

BUGS = {
    # 1. failing unit test: zero quantity rejected
    "test": ("src/shop/cart.py", "    if n >= 0:\n", "    if n > 0:\n",
             "Allow only positive quantities"),
    # 2. bad dependency version that does not exist on PyPI
    "dep": ("requirements.txt", "requests==2.32.3", "requests==2.99.0",
            "Bump requests"),
    # 3. lint error: unused import
    "lint": ("src/shop/pricing.py", "from __future__ import annotations\n",
             "from __future__ import annotations\n\nimport os\n",
             "Prepare pricing for env-based tax rate"),
}



# --------------------------------------------------------------------------- GitHub API mode
def load_token() -> str:
    token = os.getenv("GITHUB_TOKEN", "")
    env_file = pathlib.Path(__file__).resolve().parent.parent / "backend" / ".env"
    if not token and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GITHUB_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        sys.exit("GITHUB_TOKEN not found (set it in backend/.env or the environment)")
    return token


def api(token, method, url, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lazarus-break-demo",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub {method} {url} -> {e.code}: {e.read().decode()[:300]}")


def get_file(token, repo, path):
    d = api(token, "GET", f"/repos/{repo}/contents/{path}")
    return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]


def put_file(token, repo, path, content, sha, message):
    api(token, "PUT", f"/repos/{repo}/contents/{path}", {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "sha": sha,
    })
    print(f"  committed {path}: {message}")


def run_remote(bug, repo):
    token = load_token()
    if bug == "reset":
        changed = False
        for path, good in GOOD.items():
            text, sha = get_file(token, repo, path)
            if "\r\n" in text:
                good = good.replace("\n", "\r\n")
            if text != good:
                put_file(token, repo, path, good, sha, f"Reset {path} to a green build")
                changed = True
        print("Reset done." if changed else "Already green, nothing to reset.")
        return
    path, old, new, message = BUGS[bug]
    text, sha = get_file(token, repo, path)
    if "\r\n" in text:  # repo pushed from Windows with CRLF line endings
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    if old not in text:
        sys.exit(f"{path} is not in the expected state; run `reset` first")
    put_file(token, repo, path, text.replace(old, new, 1), sha, message)
    print(f"Broke '{bug}'. Watch https://github.com/{repo}/actions go red.")


# --------------------------------------------------------------------------- local clone mode
def git(repo, *args):
    print("$ git", *args)
    subprocess.run(["git", *args], cwd=repo, check=True)


def run_local(bug, repo_dir, push):
    repo = pathlib.Path(repo_dir).resolve()
    if not (repo / ".git").exists():
        sys.exit(f"{repo} is not a git clone (use --repo owner/name to edit GitHub directly)")
    if push:
        git(repo, "pull", "--rebase", "--quiet")
    if bug == "reset":
        for path, content in GOOD.items():
            (repo / path).write_bytes(content.encode("utf-8"))
        message = "Reset demo to a green build"
    else:
        path, old, new, message = BUGS[bug]
        f = repo / path
        text = f.read_bytes().decode("utf-8")
        if "\r\n" in text:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        if old not in text:
            sys.exit(f"{path} is not in the expected state; run `reset` first")
        f.write_bytes(text.replace(old, new, 1).encode("utf-8"))
    git(repo, "add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        print("Nothing changed.")
        return
    git(repo, "commit", "-m", message)
    if push:
        git(repo, "push")
    print(f"Done: {bug}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bug", choices=[*BUGS, "reset"])
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--repo", help="GitHub repo, e.g. your-user/demo-app (edits it directly)")
    where.add_argument("--repo-dir", help="local clone of the demo repo")
    ap.add_argument("--no-push", action="store_true", help="local mode: commit but don't push")
    a = ap.parse_args()
    if a.repo:
        run_remote(a.bug, a.repo)
    else:
        run_local(a.bug, a.repo_dir, not a.no_push)


if __name__ == "__main__":
    main()
