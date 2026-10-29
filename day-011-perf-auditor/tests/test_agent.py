import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import Agent
from google.genai import types

from perf_auditor.agent import (
    ASK_FOR_URL,
    MAX_CPU_THROTTLE,
    MAX_INSIGHTS,
    TOOLS,
    URL_KEY,
    _chrome_toolset,
    _make_callbacks,
    _make_resolve_url,
    build_agent,
)
from perf_auditor.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    """Stand-in for ADK's callback context: session state and the user message."""

    def __init__(self, text, state=None):
        self.state = state if state is not None else {}
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)])


def _callbacks(**kw):
    ledger = RunLedger(model="gemini-2.5-flash", **kw)
    return ledger, *_make_callbacks(ledger)


def test_before_tool_blocks_unsafe_urls_without_spending_budget():
    ledger, before_tool, _, _ = _callbacks(max_tool_calls=3)
    blocked = before_tool(_Tool("new_page"), {"url": "http://169.254.169.254/latest"}, None)
    assert "error" in blocked and ledger.tool_calls == 0
    assert before_tool(_Tool("new_page"), {"url": "https://web.dev", "isolatedContext": "x"}, None) is None
    assert ledger.tool_calls == 1


def test_before_tool_constrains_emulate_and_trace_arguments():
    _, before_tool, _, _ = _callbacks()
    args = {"pageId": 1, "cpuThrottlingRate": 20, "userAgent": "evil", "geolocation": "1,1", "networkConditions": "Fast 4G"}
    assert before_tool(_Tool("emulate"), args, None) is None
    assert args == {"pageId": 1, "cpuThrottlingRate": MAX_CPU_THROTTLE, "networkConditions": "Fast 4G"}
    trace = {"pageId": 1, "reload": False, "autoStop": False, "filePath": "/etc/cron.d/x"}
    assert before_tool(_Tool("performance_start_trace"), trace, None) is None
    assert trace == {"pageId": 1, "reload": True, "autoStop": True}


def test_before_tool_enforces_total_and_insight_budgets():
    ledger, before_tool, _, _ = _callbacks(max_tool_calls=MAX_INSIGHTS + 5)
    for _ in range(MAX_INSIGHTS):
        assert before_tool(_Tool("performance_analyze_insight"), {}, None) is None
    assert "At most" in before_tool(_Tool("performance_analyze_insight"), {}, None)["error"]
    assert ledger.tool_calls == MAX_INSIGHTS
    tiny, before_tool, _, _ = _callbacks(max_tool_calls=1)
    assert before_tool(_Tool("emulate"), {}, None) is None
    assert "budget" in before_tool(_Tool("emulate"), {}, None)["error"]


def test_after_tool_records_numbers_and_after_model_meters_tokens():
    ledger, _, after_tool, after_model = _callbacks()
    after_tool(_Tool("performance_start_trace"), {}, None, {"content": [{"text": "LCP: 315 ms\\n- CLS: 0.00"}]})
    assert ledger.has_ms(315) and ledger.has_cls(0.0)

    class _Usage:
        prompt_token_count, candidates_token_count = 100, 7

    class _Resp:
        usage_metadata = _Usage()

    after_model(None, _Resp())
    assert (ledger.model_calls, ledger.prompt_tokens, ledger.output_tokens) == (1, 100, 7)


def test_page_comes_from_each_message_not_import_time():
    """Two different inputs in one session bind correctly; a missing or unsafe one asks a question."""
    resolve = _make_resolve_url()
    state = {}
    assert resolve(_Ctx("Please audit https://web.dev", state)) is None
    assert state[URL_KEY] == "https://web.dev"
    assert resolve(_Ctx("now try example.org/pricing", state)) is None
    assert state[URL_KEY] == "https://example.org/pricing"
    for text, hint in [("hi there", ASK_FOR_URL), ("audit http://localhost:8080", "not allowed")]:
        reply = resolve(_Ctx(text, state))
        assert ASK_FOR_URL in reply.parts[0].text or hint in reply.parts[0].text
        assert state[URL_KEY] == "https://example.org/pricing"  # unchanged


def test_agent_is_one_agent_with_the_page_templated_in():
    agent = build_agent(RunLedger(model="gemini-2.5-flash"))
    assert isinstance(agent, Agent) and "{target_url}" in agent.instruction
    assert agent.before_agent_callback is not None
    assert "https://" not in agent.instruction.split("Process")[0]  # no hard-coded subject


needs_npx = pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")


@needs_npx
def test_mcp_server_exposes_exactly_the_filtered_tools():
    """Starts the real Chrome DevTools MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _chrome_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_audit_passes_all_checks(tmp_path):
    out = tmp_path / "report.md"
    result = subprocess.run(
        [sys.executable, "-m", "perf_auditor", "https://web.dev", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    report = out.read_text()
    assert "## Core Web Vitals" in report and "## Recommendations" in report
