import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.fixture_site import serve
from web_qa_explorer.agent import (
    ASK_FOR_TARGET,
    PLAYWRIGHT_TOOLS,
    TOOL_RESULT_MAX_CHARS,
    TYPE_TEXT_MAX_CHARS,
    WAIT_MAX_SECONDS,
    _make_agent_callbacks,
    _make_callbacks,
    _playwright_toolset,
    _server_args,
    _server_env,
    _truncate_result,
    build_agent,
    needs_approval,
)
from web_qa_explorer.ledger import RunLedger, unverified_evidence

TARGET = "https://shop.example.com/"


class _Tool:
    def __init__(self, name):
        self.name = name


class _Confirmation:
    def __init__(self, confirmed):
        self.confirmed = confirmed


class _ToolCtx:
    def __init__(self, confirmed=None):
        self.tool_confirmation = None if confirmed is None else _Confirmation(confirmed)


class _Ctx:
    """Stand-in for ADK's callback context: session state plus the user's message."""

    def __init__(self, state=None, text=None, invocation_id="inv-1"):
        self.state = dict(state or {})
        self.invocation_id = invocation_id
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", target_url=TARGET, **kw)


def test_navigation_outside_the_target_is_blocked_for_free():
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    result = before_tool(_Tool("browser_navigate"), {"url": "https://evil.example.net/"}, _ToolCtx())
    assert "outside the target" in result["error"]
    assert ledger.tool_calls == 0 and ledger.blocked == ["navigate https://evil.example.net/"]
    assert before_tool(_Tool("browser_navigate"), {"url": "file:///etc/passwd"}, _ToolCtx())["error"]
    assert before_tool(_Tool("browser_navigate"), {"url": "https://shop.example.com/cart"}, _ToolCtx()) is None


def test_before_tool_enforces_tool_and_page_budgets():
    before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=2, max_pages=1))
    assert before_tool(_Tool("browser_navigate"), {"url": TARGET}, _ToolCtx()) is None
    assert "Page budget" in before_tool(_Tool("browser_navigate"), {"url": TARGET + "other"}, _ToolCtx())["error"]
    assert before_tool(_Tool("browser_snapshot"), {}, _ToolCtx()) is None
    assert "budget" in before_tool(_Tool("browser_snapshot"), {}, _ToolCtx())["error"]


def test_before_tool_caps_arguments():
    before_tool, _, _ = _make_callbacks(_ledger())
    typed = {"element": "Search box", "target": "e1", "text": "x" * 5000}
    assert before_tool(_Tool("browser_type"), typed, _ToolCtx()) is None
    assert len(typed["text"]) == TYPE_TEXT_MAX_CHARS
    wait = {"time": 600}
    before_tool(_Tool("browser_wait_for"), wait, _ToolCtx())
    assert wait["time"] == WAIT_MAX_SECONDS


def test_before_tool_stops_tools_from_writing_files():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"level": "error", "filename": "console.log"}
    assert before_tool(_Tool("browser_console_messages"), args, _ToolCtx()) is None
    assert "filename" not in args


def test_only_risky_actions_ask_for_approval():
    assert needs_approval(element="Delete basket", target="e4")
    assert needs_approval(element="Password field", target="e5", text="x")
    assert not needs_approval(element="Add to basket", target="e6")
    assert not needs_approval(url="https://shop.example.com/")  # navigate carries no element label
    assert not needs_approval()  # snapshot and console tools carry no arguments at all


def test_a_risky_request_costs_no_budget_until_it_is_approved():
    ledger = _ledger(max_tool_calls=5)
    before_tool, _, _ = _make_callbacks(ledger)
    args = {"element": "Delete basket", "target": "e4"}
    assert before_tool(_Tool("browser_click"), args, _ToolCtx(confirmed=None)) is None  # the tool pauses for a human
    assert ledger.tool_calls == 0 and ledger.approvals == []
    assert before_tool(_Tool("browser_click"), args, _ToolCtx(confirmed=True)) is None
    assert ledger.tool_calls == 1 and ledger.approvals[0]["decision"] == "approved"


def test_a_declined_action_is_logged_and_spends_nothing():
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    before_tool(_Tool("browser_click"), {"element": "Delete basket", "target": "e4"}, _ToolCtx(confirmed=False))
    assert ledger.tool_calls == 0 and ledger.approvals[0]["decision"] == "declined"
    assert "browser_click(Delete basket e4)" in ledger.approvals[0]["action"]


def test_after_tool_flags_a_risky_action_that_ran_without_approval():
    """The gate is ADK's; this check proves it held, so a regression shows up in the run metrics."""
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    args = {"element": "Delete basket", "target": "e4"}
    ok = {"content": [{"type": "text", "text": "clicked"}]}
    after_tool(_Tool("browser_click"), args, _ToolCtx(confirmed=True), ok)
    assert ledger.unapproved_risky == []
    after_tool(_Tool("browser_click"), args, _ToolCtx(), {"error": "This tool call requires confirmation"})
    assert ledger.unapproved_risky == []  # paused, did not run
    after_tool(_Tool("browser_click"), args, _ToolCtx(), ok)
    assert len(ledger.unapproved_risky) == 1


def test_after_tool_truncates_and_records_only_what_the_model_sees():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    big = {"content": [{"type": "text", "text": "A" * (TOOL_RESULT_MAX_CHARS + 500) + "SECRET_TAIL"}]}
    replaced = after_tool(_Tool("browser_snapshot"), {}, _ToolCtx(), big)
    assert replaced["content"][0]["text"].endswith("[truncated]")
    assert "SECRET_TAIL" not in ledger.evidence[0]  # the model never saw it, so it can't count as evidence
    small = {"content": [{"type": "text", "text": "ok"}]}
    assert after_tool(_Tool("browser_snapshot"), {}, _ToolCtx(), small) is None
    assert _truncate_result("not a dict") == ("not a dict", False)


def test_target_comes_from_the_message_not_import_time():
    """Two targets in one session: each message rebinds the agent."""
    ledger = _ledger()
    resolve = _make_agent_callbacks(ledger)
    ctx = _Ctx(text="https://demo.playwright.dev/todomvc/ add two todos")
    assert resolve(ctx) is None
    assert ctx.state["target_url"] == "https://demo.playwright.dev/todomvc/" and ctx.state["focus"] == "add two todos"
    assert ledger.target_url == ctx.state["target_url"]
    ctx2 = _Ctx(state=ctx.state, text="Test https://other.example.com/login", invocation_id="inv-2")
    assert resolve(ctx2) is None
    assert ctx2.state["target_url"] == "https://other.example.com/login" and ledger.target_url == ctx2.state["target_url"]
    assert ctx2.state["focus"] == "Test"


def test_each_run_gets_a_fresh_budget_and_evidence():
    """`adk web` shares one ledger across messages; a spent budget or old evidence must not carry over."""
    ledger = _ledger()
    resolve = _make_agent_callbacks(ledger)
    resolve(_Ctx(text="https://a.example.com/", invocation_id="inv-1"))
    for _ in range(ledger.max_tool_calls):
        ledger.record_tool_call("browser_click")
    ledger.record_evidence("old")
    resolve(_Ctx(text="https://b.example.com/", invocation_id="inv-2"))
    assert ledger.tool_calls == 0 and ledger.evidence == [] and ledger.record_tool_call("browser_click")


def test_resuming_after_an_approval_keeps_the_run_state():
    """ADK re-runs the agent callback with the original message when it resumes a paused invocation."""
    ledger = _ledger()
    resolve = _make_agent_callbacks(ledger)
    message = "https://shop.example.com/ add an item"
    resolve(_Ctx(text=message, invocation_id="inv-1"))
    ledger.record_tool_call("browser_click")
    ledger.record_evidence("seen")
    ledger.record_approval("browser_click(Delete)", True)
    resolve(_Ctx(text=message, invocation_id="inv-1"))  # the resume
    assert ledger.tool_calls == 1 and ledger.evidence == ["seen"] and len(ledger.approvals) == 1
    resolve(_Ctx(text=message, invocation_id="inv-2"))  # the same text as a new request is a new run
    assert ledger.tool_calls == 0 and ledger.evidence == []


def test_asks_for_a_url_when_none_given_but_keeps_the_target_on_a_follow_up():
    resolve = _make_agent_callbacks(_ledger())
    assert resolve(_Ctx(text="hello")).parts[0].text == ASK_FOR_TARGET
    state = {"target_url": TARGET}
    assert resolve(_Ctx(state=state, text="ok")) is None  # e.g. an approval reply on the same run


def test_agent_reads_its_target_from_state_and_exposes_one_filtered_toolset():
    agent = build_agent(_ledger())
    assert "{target_url}" in agent.instruction and "{focus}" in agent.instruction
    assert "shop.example.com" not in agent.instruction  # nothing about the subject is hard-coded
    assert len(agent.tools) == 1  # one server, one browser: a second toolset would open a second browser
    assert agent.tools[0].require_confirmation is needs_approval


def test_server_is_pinned_and_sandbox_is_only_disabled_for_root():
    assert any(a.startswith("@playwright/mcp@") and a[len("@playwright/mcp@"):][0].isdigit() for a in _server_args())
    assert ("--no-sandbox" in _server_args()) == (hasattr(os, "geteuid") and os.geteuid() == 0)


needs_npx = pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")


async def _browse(fn):
    """Run `fn(session)` against a real Playwright MCP server using the agent's own launch settings."""
    params = StdioServerParameters(command="npx", args=_server_args(), env=_server_env())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


@needs_npx
def test_playwright_exposes_exactly_the_filtered_tools():
    """Starts the real Playwright MCP server (no API key needed)."""

    async def names():
        toolset = _playwright_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(PLAYWRIGHT_TOOLS)


@needs_npx
def test_real_browser_surfaces_fixture_defects_that_pass_the_evidence_check():
    """Real browser, local fixture with a console error and a failing request.

    Feeds the actual tool output through the agent's callbacks and checks that a report quoting it
    verifies, while a report with an invented quote does not.
    """
    with serve() as url:
        ledger = RunLedger(model="gemini-2.5-flash", target_url=url)
        _, after_tool, _ = _make_callbacks(ledger)

        async def explore(session):
            for name, args in [
                ("browser_navigate", {"url": url}),
                ("browser_console_messages", {"level": "error"}),
                ("browser_network_requests", {}),
            ]:
                result = await session.call_tool(name, args)
                after_tool(_Tool(name), args, _ToolCtx(), {"content": [{"type": "text", "text": _text(result)}]})

        asyncio.run(_browse(explore))

    seen = "\n".join(ledger.evidence)
    assert "Cart widget failed: TypeError: cart is undefined" in seen
    assert "/api/prices" in seen and "500" in seen
    real = (
        "## Summary\nx\n## Findings\n### BUG-1 · Cart crashes\n"
        "- **Severity:** high\n- **Evidence:** `Cart widget failed: TypeError: cart is undefined`\n## Coverage\nx"
    )
    invented = real.replace("cart is undefined", "basket is null")
    assert unverified_evidence(real, ledger) == []
    assert unverified_evidence(invented, ledger) == ["Cart widget failed: TypeError: basket is null"]


LIVE_TARGET = "https://demo.playwright.dev/todomvc/"
LIVE_FOCUS = "add two todos, complete one, delete one, and check the console and network for defects"


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
@needs_npx
def test_live_report_has_verified_evidence_and_gated_actions(tmp_path):
    """Full run with Gemini and a real browser. Deleting a todo needs approval, and nobody approves it here."""
    out, metrics = tmp_path / "report.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "web_qa_explorer", LIVE_TARGET, "--focus", LIVE_FOCUS, "--risky", "deny",
         "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    (tmp_path / "stderr.txt").write_text(result.stderr)  # kept so the run's meter line can be quoted
    assert result.returncode == 0, result.stderr
    report = out.read_text()
    assert "## Findings" in report and "## Coverage" in report
    checks = json.loads(metrics.read_text())["checks"]
    assert checks["evidence_verified"] and checks["risky_actions_gated"]
