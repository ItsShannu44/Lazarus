"""Big source files: only the lines around the error go to the LLM, and the fix still applies."""
import asyncio

from app.agents import error_lines, file_view

from .fakes import FakeLLM
from .test_pipeline import make, run_pipeline


def big_js(bug_line=1200, n=1700):
    lines = [f"function helper{i}() {{ return {i}; }}" for i in range(1, n + 1)]
    lines[bug_line - 1] = "    new Markd ownApp();"
    return "\n".join(lines) + "\n"


def test_error_lines_understands_common_log_formats():
    assert error_lines("src/app.js", "/home/runner/work/x/x/src/app.js:106\nSyntaxError") == [106]
    assert error_lines("app.py", 'File "/w/app.py", line 42, in <module>') == [42]
    assert error_lines("js/app.js", "SyntaxError in app.js at line 106 caused by") == [106]
    assert error_lines("tests/test_x.py", "tests/test_x.py:15: AssertionError") == [15]
    assert error_lines("other.js", "app.js:5") == []


def test_big_file_is_cut_to_a_window_around_the_error():
    content = big_js()
    view = file_view("src/app.js", content, "src/app.js:1200 SyntaxError")
    assert len(content) > 50_000 and len(view) < 6_000
    assert "new Markd ownApp();" in view
    assert "<<< lines 1-1139 not shown >>>" in view and view.rstrip().endswith("not shown >>>")
    assert file_view("small.js", "x()\n", "small.js:1") == "x()\n"  # small files untouched


def test_fix_on_a_big_file_applies_to_the_full_file():
    fix = {"reasoning": "typo", "confidence": 92,
           "edits": [{"path": "src/app.js", "search": "    new Markd ownApp();", "replace": "    new MarkdownApp();"}]}

    class LogSays(FakeLLM):
        async def json(self, system, prompt, schema, agent=""):
            if agent == "Fixer":
                assert len(prompt) < 12_000, f"prompt too big: {len(prompt)}"
            out = await super().json(system, prompt, schema, agent)
            if agent == "Log Analyst":
                out.update(summary="SyntaxError in app.js at line 1200", suspected_files=["src/app.js"])
            if agent == "Code Locator":
                out = {"files": [{"path": "src/app.js", "reason": "error here"}]}
            return out

    orch, gh, _ = make()
    orch.llm = LogSays(fixes=[fix])
    gh.files = {"src/app.js": big_js(), "README.md": "# x"}

    async def logs(repo, job_id):
        return "/home/runner/work/js/js/src/app.js:1200\n    new Markd ownApp();\nSyntaxError: Unexpected identifier 'ownApp'\n"
    gh.job_logs = logs
    run = run_pipeline(orch)
    assert run.status == "awaiting_approval", run.escalation_reason
    committed = next(iter(gh.commits.values()))["files"]["src/app.js"][0]
    assert "new MarkdownApp();" in committed and committed.count("\n") == big_js().count("\n")
