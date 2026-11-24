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

from a11y_auditor.agent import (
    ALLOWED_KEYS,
    ASK_FOR_TARGET,
    PLAYWRIGHT_TOOLS,
    TOOL_RESULT_MAX_CHARS,
    _make_agent_callbacks,
    _make_callbacks,
    _playwright_toolset,
    _server_args,
    _server_env,
    _truncate_result,
    build_agent,
)
from a11y_auditor.ledger import RunLedger
from a11y_auditor.rubric import RUBRIC
from tests.fixture_site import serve

TARGET = "https://shop.example.com/"


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    """Stand-in for ADK's callback context: session state plus the user's message."""

    def __init__(self, state=None, text=None, invocation_id="inv-1"):
        self.state = dict(state or {})
        self.invocation_id = invocation_id
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-3.5-flash-lite", target_url=TARGET, **kw)


def test_navigation_outside_the_target_is_blocked_for_free():
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    result = before_tool(_Tool("browser_navigate"), {"url": "https://evil.example.net/"}, None)
    assert "outside the target" in result["error"]
    assert ledger.tool_calls == 0 and ledger.blocked == ["navigate https://evil.example.net/"]
    assert before_tool(_Tool("browser_navigate"), {"url": "file:///etc/passwd"}, None)["error"]
    assert before_tool(_Tool("browser_navigate"), {"url": TARGET + "cart"}, None) is None


def test_before_tool_enforces_tool_and_page_budgets():
    before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=2, max_pages=1))
    assert before_tool(_Tool("browser_navigate"), {"url": TARGET}, None) is None
    assert "Page budget" in before_tool(_Tool("browser_navigate"), {"url": TARGET + "other"}, None)["error"]
    assert before_tool(_Tool("browser_snapshot"), {}, None) is None
    assert "budget" in before_tool(_Tool("browser_snapshot"), {}, None)["error"]


def test_only_tab_keys_are_allowed_and_a_rejected_key_costs_nothing():
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    assert before_tool(_Tool("browser_press_key"), {"key": "Tab"}, None) is None
    assert before_tool(_Tool("browser_press_key"), {"key": "Shift+Tab"}, None) is None
    for key in ("Enter", "Space", "Control+A", None):
        assert "read-only" in before_tool(_Tool("browser_press_key"), {"key": key}, None)["error"]
    assert ledger.tool_calls == 2 and ALLOWED_KEYS == {"Tab", "Shift+Tab"}


def test_tools_cannot_write_files():
    args = {"filename": "/etc/cron.d/x"}
    _make_callbacks(_ledger())[0](_Tool("browser_snapshot"), args, None)
    assert args == {}


def test_after_tool_truncates_and_records_only_what_the_model_sees():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    big = {"content": [{"type": "text", "text": "x" * (TOOL_RESULT_MAX_CHARS + 500) + "TAILMARK"}]}
    trimmed = after_tool(_Tool("browser_snapshot"), {}, None, big)
    assert trimmed["content"][0]["text"].endswith("[truncated]")
    assert "TAILMARK" not in "\n".join(ledger.evidence)
    small = {"content": [{"type": "text", "text": "- button [ref=e7]"}]}
    assert after_tool(_Tool("browser_snapshot"), {}, None, small) is None
    assert "- button [ref=e7]" in ledger.evidence[-1]
    assert _truncate_result("not a dict") == ("not a dict", False)


def test_target_comes_from_the_message_not_import_time():
    ledger = _ledger()
    resolve = _make_agent_callbacks(ledger)
    first, second = _Ctx(text="audit https://a.example/"), _Ctx(text="now https://b.example/page", invocation_id="inv-2")
    assert resolve(first) is None and resolve(second) is None
    assert first.state["target_url"] == "https://a.example/" and second.state["target_url"] == "https://b.example/page"
    assert ledger.target_url == "https://b.example/page" and ledger.invocation_id == "inv-2"


def test_each_run_gets_a_fresh_budget_and_evidence():
    ledger = _ledger()
    ledger.record_tool_call("browser_snapshot")
    ledger.record_evidence("old run")
    ledger.record_page(TARGET)
    _make_agent_callbacks(ledger)(_Ctx(text="https://b.example/"))
    assert (ledger.tool_calls, ledger.evidence, ledger.pages, ledger.calls_by_tool) == (0, [], [], {})


def test_asks_for_a_url_when_none_given_but_keeps_the_target_on_a_follow_up():
    resolve = _make_agent_callbacks(_ledger())
    ask = resolve(_Ctx(text="please audit my site"))
    assert ask.parts[0].text == ASK_FOR_TARGET
    assert resolve(_Ctx(state={"target_url": TARGET}, text="and the footer?")) is None


def test_agent_reads_its_target_from_state_and_exposes_one_filtered_toolset():
    agent = build_agent(_ledger())
    assert "{target_url}" in agent.instruction
    assert all(c.id in agent.instruction for c in RUBRIC)  # the rubric is in the prompt
    assert len(agent.tools) == 1 and agent.tools[0].tool_filter == PLAYWRIGHT_TOOLS


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
def test_real_browser_snapshot_shows_the_fixture_defects():
    """Real browser on a local page with known defects: the snapshot is the evidence the agent will quote."""
    with serve() as url:
        ledger = RunLedger(model="m", target_url=url)
        _, after_tool, _ = _make_callbacks(ledger)

        async def audit(session):
            for name, args in [("browser_navigate", {"url": url}), ("browser_snapshot", {}),
                               ("browser_press_key", {"key": "Tab"}), ("browser_snapshot", {})]:
                result = await session.call_tool(name, args)
                after_tool(_Tool(name), args, None, {"content": [{"type": "text", "text": _text(result)}]})

        asyncio.run(_browse(audit))

    seen = "\n".join(ledger.evidence)
    assert 'link "click here"' in seen and "- textbox [ref=" in seen and "- button [ref=" in seen
    assert "- link [active]" in seen  # Tab moved focus to the first link, the one that holds only an image
    assert "Page Title" not in seen  # the page has an empty title, so nothing is shown for it


LIVE_TARGET = "https://demo.playwright.dev/todomvc/"


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
@needs_npx
def test_live_audit_grades_the_whole_rubric_with_verified_evidence(tmp_path):
    """Full run with Gemini and a real browser against a public demo page."""
    out, metrics = tmp_path / "report.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "a11y_auditor", LIVE_TARGET, "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    (tmp_path / "stderr.txt").write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    checks = json.loads(metrics.read_text())["checks"]
    assert checks["rubric_complete"] and checks["evidence_verified"]
