"""Log cleaning, path extraction and search/replace patching."""
from __future__ import annotations

import difflib
import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")
ERRORISH = re.compile(
    r"(##\[error\]|error|failed|failure|exception|traceback|assert|FAIL|E\s{3}|"
    r"No matching distribution|Could not find a version|not found|undefined|"
    r"cannot|unexpected|SyntaxError|TypeError|ImportError|ModuleNotFound|F\d{3}\b|E\d{3}\b)",
    re.I,
)
NOISE = re.compile(
    r"^(##\[(group|endgroup)\]|Requirement already satisfied|\s*Downloading |\s*Using cached |"
    r"\[command\]|hint:|Temporarily overriding HOME|Adding repository directory|Deleting the contents of|"
    r"Initialized empty Git repository|From https://github.com|\s*\* \[new ref\]|Switched to a new branch|"
    r"branch '.*' set up to track|git version |Cleaning up orphan processes|\(node:\d+\)|\(Use `node|"
    r"##\[warning\]Node\.js|Syncing repository|Getting Git version|Fetching the repository|"
    r"Determining the checkout info|Checking out the ref|Successfully set up |"
    r"Collecting [\w.-]+|\s*(pythonLocation|PKG_CONFIG_PATH|Python\d?_ROOT_DIR|LD_LIBRARY_PATH|"
    r"check-latest|token|update-environment|allow-prereleases|freethreaded|python-version):|with:$|env:$|"
    r"shell: )"
)
STOP = re.compile(r"^(Post job cleanup|Post Run actions/)")  # everything after is teardown
MAX_LINE = 320
PATHISH = re.compile(r"[\w./\\-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|txt|toml|cfg|ini|json|ya?ml)\b")


SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)['\"]?[^\s'\"]{6,}"),
    re.compile(r"mongodb(?:\+srv)?://[^\s]+"),
]


def redact_secrets(text: str) -> str:
    """Hide anything that looks like a credential before it reaches an LLM, a PR or an issue."""
    for pat in SECRET_PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


def norm_path(p: str) -> str:
    """'./src/x.py' or '/src/x.py' -> 'src/x.py' (keeps dot-files like .github/ intact)."""
    p = str(p).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$")
CHEATS = [
    (re.compile(r"#\s*noqa|#\s*type:\s*ignore|#\s*pragma:\s*no cover|pylint:\s*disable"), "silences a linter/type checker"),
    (re.compile(r"eslint-disable|@ts-ignore|@ts-nocheck|@ts-expect-error"), "silences a linter/type checker"),
    (re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|unittest\.skip|@skip\b"), "skips a test"),
    (re.compile(r"\b(it|test|describe)\.(skip|todo)\(|\bx(it|describe)\("), "skips a test"),
    (re.compile(r"continue-on-error|\|\|\s*true\b|exit 0"), "hides a failing command"),
]
ASSERTISH = re.compile(r"\bassert\b|\bexpect\(|\bassert[A-Z]\w*\(|\bself\.assert\w*\(|pytest\.raises")


def is_test_file(path: str) -> bool:
    return bool(TEST_FILE.search(path))


def cheat_reason(path: str, before: str, after: str) -> str | None:
    """Why this change games CI instead of fixing it, or None if it looks honest."""
    nl = lambda t: t if t.endswith("\n") else t + "\n"  # noqa: E731  (difflib merges lines without newline)
    diff = unified_diff(path, nl(before), nl(after))
    added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    for line in added:
        for pat, why in CHEATS:
            if pat.search(line):
                return f"{path}: the change {why} (`{line.strip()[:60]}`)"
    if is_test_file(path):
        test_def = r"^\s*(?:async\s+)?def test_|^\s*(?:it|test)\("
        if len(re.findall(test_def, after, re.M)) < len(re.findall(test_def, before, re.M)):
            return f"{path}: the change deletes a test"
        if len(ASSERTISH.findall(after)) < len(ASSERTISH.findall(before)):
            return f"{path}: the change removes test assertions"
    return None


def clean_log(raw: str) -> list[str]:
    """Strip timestamps, colours and runner chatter so the model only pays for real output."""
    out = []
    for line in raw.splitlines():
        line = TIMESTAMP.sub("", ANSI.sub("", line)).rstrip()
        if STOP.match(line):
            break
        if not line or NOISE.match(line):
            continue
        if len(line) > MAX_LINE:  # e.g. pip listing every version ever released
            line = line[: MAX_LINE - 40] + " …[trimmed]… " + line[-30:]
        out.append(line)
    return out


def log_excerpt(raw: str, max_chars: int = 12000, tail: int = 80, context: int = 3) -> str:
    """Error lines with a little context, plus the tail of the log, in original order."""
    lines = clean_log(redact_secrets(raw))
    keep: set[int] = set(range(max(0, len(lines) - tail), len(lines)))
    hits = [i for i, l in enumerate(lines) if ERRORISH.search(l)]
    for i in hits[:25]:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    chunks, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1:
            chunks.append("...")
        chunks.append(lines[i])
        prev = i
    text = "\n".join(chunks)
    if len(text) > max_chars:  # keep the end: that's where the failure usually is
        text = "...\n" + text[-max_chars:]
    return text


_PYTEST = re.compile(r"(\d+) (passed|failed|error|errors)\b")
_JEST = re.compile(r"Tests:\s+(?:(\d+) failed, )?(?:\d+ skipped, )?(?:(\d+) passed, )?(\d+) total")
_MOCHA = re.compile(r"(\d+) (passing|failing)\b")


def count_tests(log: str) -> dict[str, int]:
    """Best-effort pass/fail counts from pytest, jest/vitest or mocha output. {} if unknown."""
    text = "\n".join(clean_log(log))
    m = _JEST.search(text)
    if m:
        failed, passed, total = int(m.group(1) or 0), int(m.group(2) or 0), int(m.group(3))
        return {"passed": passed, "failed": failed, "total": total}
    # pytest prints the summary last; take the last line that has counts
    for line in reversed(text.splitlines()):
        found = _PYTEST.findall(line)
        if found and ("passed" in line or "failed" in line) and (" in " in line or "=" in line):
            c = {"passed": 0, "failed": 0}
            for n, kind in found:
                c["passed" if kind == "passed" else "failed"] += int(n)
            c["total"] = c["passed"] + c["failed"]
            return c
    mocha = dict((k, int(n)) for n, k in _MOCHA.findall(text))
    if mocha:
        p, f = mocha.get("passing", 0), mocha.get("failing", 0)
        return {"passed": p, "failed": f, "total": p + f}
    return {}


def paths_in_text(text: str, repo_paths: list[str]) -> list[str]:
    """Repo files mentioned in a log (runner paths are absolute, so match by suffix)."""
    found: list[str] = []
    for m in PATHISH.findall(text):
        m = m.replace("\\", "/").lstrip("./")
        for p in repo_paths:
            if (m == p or m.endswith("/" + p)) and p not in found:
                found.append(p)
    return found


class PatchError(Exception):
    pass


def _loose_find(content: str, search: str) -> tuple[int, int] | None:
    """Match ignoring trailing whitespace on each line. Returns (start, end) or None."""
    c_lines = content.splitlines(keepends=True)
    s_lines = [l.rstrip() for l in search.strip("\n").splitlines()]
    if not s_lines:
        return None
    matches = []
    for i in range(len(c_lines) - len(s_lines) + 1):
        if all(c_lines[i + j].rstrip() == s_lines[j] for j in range(len(s_lines))):
            matches.append(i)
    if len(matches) != 1:
        return None
    i = matches[0]
    start = sum(len(l) for l in c_lines[:i])
    end = start + sum(len(l) for l in c_lines[i : i + len(s_lines)])
    return start, end


def apply_edit(content: str, search: str, replace: str, path: str) -> str:
    if not search:
        raise PatchError(f"{path}: empty search string")
    count = content.count(search)
    if count == 1:
        return content.replace(search, replace, 1)
    if count > 1:
        raise PatchError(f"{path}: search text appears {count} times; include more context")
    span = _loose_find(content, search)
    if span is None:
        raise PatchError(f"{path}: search text not found verbatim in the file")
    start, end = span
    block = content[start:end]
    rep = replace.strip("\n") + ("\n" if block.endswith("\n") else "")
    return content[:start] + rep + content[end:]


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def numbered(content: str, max_chars: int = 20000) -> str:
    text = content if len(content) <= max_chars else content[:max_chars] + "\n...[truncated]"
    return "\n".join(f"{i+1:>4}| {l}" for i, l in enumerate(text.splitlines()))
