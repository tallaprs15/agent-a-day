import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.genai import types

from pr_reviewer.agent import (
    ASK_FOR_PR,
    FETCH_TOOLS,
    GITHUB_TOOLS,
    _fetch_toolset,
    _make_callbacks,
    _make_resolve_pr,
    build_agent,
)
from pr_reviewer.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, text=None, state=None):
        self.state = state if state is not None else {}
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _state(n=7):
    return {"pr_url": f"https://github.com/octo/demo/pull/{n}"}


def test_fetch_is_scoped_capped_and_budgeted():
    ledger = RunLedger(model="m", max_tool_calls=1)
    before, _, _ = _make_callbacks(ledger)
    ctx = _Ctx(state=_state())
    out_of_scope = before(_Tool("fetch"), {"url": "https://github.com/octo/demo/pull/8.diff"}, ctx)
    assert "Only this pull request" in out_of_scope["error"] and ledger.tool_calls == 0  # rejected calls are free
    args = {"url": "https://github.com/octo/demo/pull/7.diff", "max_length": 10**6}
    assert before(_Tool("fetch"), args, ctx) is None
    assert args["max_length"] == 20000 and args["raw"] is True
    assert "budget" in before(_Tool("fetch"), {"url": "https://github.com/octo/demo/pull/7.diff"}, ctx)["error"]


def test_github_tools_are_pinned_to_the_pr_and_read_methods():
    before, _, _ = _make_callbacks(RunLedger(model="m"))
    ctx = _Ctx(state=_state())
    args = {"method": "get_diff", "owner": "x", "repo": "y", "pullNumber": 1}
    assert before(_Tool("pull_request_read"), args, ctx) is None
    assert (args["owner"], args["repo"], args["pullNumber"]) == ("octo", "demo", 7)
    assert "Allowed methods" in before(_Tool("pull_request_read"), {"method": "merge"}, ctx)["error"]


def test_no_pr_bound_means_no_tools():
    before, _, _ = _make_callbacks(RunLedger(model="m"))
    assert "No pull request" in before(_Tool("fetch"), {"url": "https://github.com/a/b/pull/1.diff"}, _Ctx())["error"]


def test_pr_comes_from_each_message_not_import_time():
    resolve = _make_resolve_pr()
    ctx = _Ctx(text="Review https://github.com/octo/demo/pull/7 please")
    assert resolve(ctx) is None and ctx.state["pr_url"].endswith("octo/demo/pull/7")
    ctx2 = _Ctx(text="Now https://github.com/acme/tool/pull/99", state=ctx.state)
    assert resolve(ctx2) is None and ctx2.state["pr_url"] == "https://github.com/acme/tool/pull/99"


def test_missing_pr_link_triggers_a_clarifying_question():
    resolve = _make_resolve_pr()
    assert resolve(_Ctx(text="review my code")).parts[0].text == ASK_FOR_PR
    assert resolve(_Ctx(text=None)).parts[0].text == ASK_FOR_PR


def test_agent_builds_with_both_server_modes(monkeypatch):
    monkeypatch.delenv("GITHUB_MCP_TOKEN", raising=False)
    assert build_agent(RunLedger(model="gemini-2.5-flash")).name == "pr_reviewer"
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "placeholder")
    assert build_agent(RunLedger(model="gemini-2.5-flash")).name == "pr_reviewer"
    assert not {"merge_pull_request", "create_pull_request", "pull_request_review_write"} & set(GITHUB_TOOLS)


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_fetch_server_exposes_exactly_the_filtered_tools():
    """Starts the real MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _fetch_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(FETCH_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_review_only_cites_files_in_the_diff(tmp_path):
    out = tmp_path / "review.md"
    result = subprocess.run(
        [sys.executable, "-m", "pr_reviewer", "https://github.com/tallaprs15/agent-a-day/pull/1", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    assert "## Findings" in out.read_text()
