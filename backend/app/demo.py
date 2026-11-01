"""Stage-demo controls: break the demo repo in one of three ways, or reset it.

Same bugs as scripts/break_demo.py, but callable from the dashboard.
Only acts on repos in ALLOWED_REPOS.
"""
from __future__ import annotations

from .github_client import GitHub, GitHubError

GOOD: dict[str, str] = {
    "src/shop/cart.py": "\"\"\"A tiny shopping cart used to demo Lazarus.\"\"\"\nfrom __future__ import annotations\n\nfrom dataclasses import dataclass, field\n\n\n@dataclass\nclass Item:\n    name: str\n    price: float\n    qty: int = 1\n\n\ndef check(n: int) -> bool:\n    \"\"\"Quantities may be zero (item kept in cart) but never negative.\"\"\"\n    if n >= 0:\n        return True\n    return False\n\n\n@dataclass\nclass Cart:\n    items: list[Item] = field(default_factory=list)\n\n    def add(self, name: str, price: float, qty: int = 1) -> None:\n        if not check(qty):\n            raise ValueError(\"quantity cannot be negative\")\n        self.items.append(Item(name, price, qty))\n\n    def total(self) -> float:\n        return round(sum(i.price * i.qty for i in self.items), 2)\n\n    def apply_discount(self, percent: float) -> float:\n        if not 0 <= percent <= 100:\n            raise ValueError(\"discount must be between 0 and 100\")\n        return round(self.total() * (1 - percent / 100), 2)\n",
    "src/shop/pricing.py": "\"\"\"Price helpers.\"\"\"\nfrom __future__ import annotations\n\nTAX_RATE = 0.18  # GST\n\n\ndef with_tax(amount: float) -> float:\n    return round(amount * (1 + TAX_RATE), 2)\n\n\ndef format_inr(amount: float) -> str:\n    return f\"₹{amount:,.2f}\"\n",
    "requirements.txt": "pytest==8.3.3\nruff==0.6.9\nrequests==2.32.3\n"
}

# bug -> (path, healthy text, broken text, commit message, label)
BUGS: dict[str, tuple[str, str, str, str, str]] = {
    "test": ("src/shop/cart.py", "    if n >= 0:\n", "    if n > 0:\n",
             "Allow only positive quantities", "Failing unit test"),
    "dep": ("requirements.txt", "requests==2.32.3", "requests==2.99.0",
            "Bump requests", "Bad dependency version"),
    "lint": ("src/shop/pricing.py", "from __future__ import annotations\n",
             "from __future__ import annotations\n\nimport os\n",
             "Prepare pricing for env-based tax rate", "Lint error"),
}


class DemoError(Exception):
    pass


NOT_DEMO = (
    "The stage-demo buttons only work on a copy of demo-app: this repository doesn't have "
    "src/shop/cart.py, src/shop/pricing.py and requirements.txt. Real failures in it are still fixed automatically."
)


async def _get(gh: GitHub, repo: str, path: str) -> tuple[str, str]:
    try:
        return await gh.get_contents(repo, path)
    except GitHubError as e:
        if e.status == 404:
            raise DemoError(NOT_DEMO)
        raise


async def is_demo_repo(gh: GitHub, repo: str) -> bool:
    try:
        for path in GOOD:
            await gh.get_contents(repo, path)
        return True
    except GitHubError:
        return False


def _eol(text: str, s: str) -> str:
    return s.replace("\n", "\r\n") if "\r\n" in text else s


async def break_repo(gh: GitHub, repo: str, bug: str) -> str:
    if bug not in BUGS:
        raise DemoError(f"unknown bug '{bug}'")
    path, old, new, message, _ = BUGS[bug]
    text, sha = await _get(gh, repo, path)
    old, new = _eol(text, old), _eol(text, new)
    if old not in text:
        if new in text:
            raise DemoError(f"'{bug}' is already broken. Wait for Lazarus or press Reset.")
        raise DemoError(f"{path} is not in the expected state. Press Reset first.")
    await gh.put_contents(repo, path, text.replace(old, new, 1), sha, message)
    return message


async def reset_repo(gh: GitHub, repo: str) -> list[str]:
    changed = []
    for path, good in GOOD.items():
        text, sha = await _get(gh, repo, path)
        good = _eol(text, good)
        if text != good:
            await gh.put_contents(repo, path, good, sha, f"Reset {path} to a green build")
            changed.append(path)
    return changed
