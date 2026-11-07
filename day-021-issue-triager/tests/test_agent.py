import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest
from google.genai import types

from issue_triager.agent import ASK_FOR_ISSUE, FETCH_TOOLS, _fetch_toolset, _make_callbacks, _make_resolve_issue, build_agent
from issue_triager.ledger import RunLedger

API = "https://api.github.com"


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, text=None, state=None):
        self.state = state if state is not None else {}
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _state(n=7):
    return {"issue_url": f"https://github.com/octo/demo/issues/{n}"}


def test_fetch_is_scoped_capped_and_budgeted():
    ledger = RunLedger(model="m", max_tool_calls=1)
    before, _, _ = _make_callbacks(ledger)
    ctx = _Ctx(state=_state())
    out_of_scope = before(_Tool("fetch"), {"url": API + "/repos/octo/other/issues/7"}, ctx)
    assert "Only issue data" in out_of_scope["error"] and ledger.tool_calls == 0  # rejected calls are free
    args = {"url": API + "/repos/octo/demo/issues?state=all&per_page=500", "max_length": 5}
    assert before(_Tool("fetch"), args, ctx) is None
    assert "per_page=20" in args["url"] and args["raw"] is True and args["max_length"] > 100_000
    assert "budget" in before(_Tool("fetch"), {"url": API + "/repos/octo/demo/labels"}, ctx)["error"]


def test_search_without_type_is_rejected_for_free():
    ledger = RunLedger(model="m")
    before, _, _ = _make_callbacks(ledger)
    out = before(_Tool("fetch"), {"url": API + "/search/issues?q=crash+repo:octo/demo"}, _Ctx(state=_state()))
    assert "is:issue" in out["error"] and ledger.tool_calls == 0


def test_no_issue_bound_means_no_tools():
    before, _, _ = _make_callbacks(RunLedger(model="m"))
    assert "No issue" in before(_Tool("fetch"), {"url": API + "/repos/a/b/issues/1"}, _Ctx())["error"]


def test_after_tool_slims_the_response_and_records_evidence():
    ledger = RunLedger(model="m")
    _, after, _ = _make_callbacks(ledger)
    raw = json.dumps({"number": 7, "title": "T", "user": {"login": "alice"}, "labels": [], "body": "b", "node_id": "noise"})
    out = after(_Tool("fetch"), {}, _Ctx(state=_state()), {"content": [{"type": "text", "text": "Contents of u:\n" + raw}]})
    assert "noise" not in out["content"][0]["text"] and ledger.target_seen and ledger.seen_logins == {"alice"}
    assert after(_Tool("fetch"), {}, _Ctx(state=_state()), {"content": [{"type": "text", "text": "<html>"}]}) is None


def test_issue_comes_from_each_message_not_import_time():
    resolve = _make_resolve_issue()
    ctx = _Ctx(text="Triage https://github.com/octo/demo/issues/7 please")
    assert resolve(ctx) is None and ctx.state["issue_url"].endswith("octo/demo/issues/7")
    ctx2 = _Ctx(text="Now https://github.com/acme/tool/issues/99", state=ctx.state)
    assert resolve(ctx2) is None and ctx2.state["issue_url"] == "https://github.com/acme/tool/issues/99"
    # a PR link is bound as the same issue number
    ctx3 = _Ctx(text="https://github.com/acme/tool/pull/5", state=ctx.state)
    assert resolve(ctx3) is None and ctx3.state["issue_url"] == "https://github.com/acme/tool/issues/5"


def test_missing_issue_link_triggers_a_clarifying_question():
    resolve = _make_resolve_issue()
    assert resolve(_Ctx(text="what is wrong with my repo")).parts[0].text == ASK_FOR_ISSUE
    assert resolve(_Ctx(text=None)).parts[0].text == ASK_FOR_ISSUE


def test_root_agent_does_not_hard_code_a_subject():
    agent = build_agent(RunLedger(model="gemini-3.5-flash-lite"))
    assert agent.name == "issue_triager" and "{issue_url}" in agent.instruction and "github.com/octo" not in agent.instruction


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
def test_live_triage_only_names_labels_issues_and_people_the_tools_returned(tmp_path):
    out = tmp_path / "triage.md"
    result = subprocess.run(
        [sys.executable, "-m", "issue_triager", "https://github.com/tallaprs15/agent-a-day/issues/2", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    assert "## Labels" in out.read_text()
