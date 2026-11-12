import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import SequentialAgent

from deep_planner.agent import (
    ASK_FOR_GOAL,
    MAX_THOUGHTS,
    THINKING_TOOLS,
    THOUGHT_MAX_CHARS,
    _make_callbacks,
    _make_pipeline_callbacks,
    _thinking_toolset,
    build_agent,
)
from deep_planner.ledger import Plan, RunLedger


class _Tool:
    name = "sequentialthinking"


class _Ctx:
    """Stand-in for ADK's callback context: session state plus the user's message."""

    def __init__(self, state=None, text=None):
        from google.genai import types

        self.state = dict(state or {})
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


def test_before_tool_enforces_budget():
    before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=1))
    args = {"thought": "a", "thoughtNumber": 1, "totalThoughts": 4, "nextThoughtNeeded": True}
    assert before_tool(_Tool(), args, None) is None
    assert "budget" in before_tool(_Tool(), dict(args), None)["error"]


def test_before_tool_caps_thought_length_and_total():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"thought": "x" * 9000, "thoughtNumber": 1, "totalThoughts": 500, "nextThoughtNeeded": True}
    assert before_tool(_Tool(), args, None) is None
    assert len(args["thought"]) == THOUGHT_MAX_CHARS and args["totalThoughts"] == MAX_THOUGHTS
    args = {"thought": "a", "thoughtNumber": 1, "totalThoughts": 1, "nextThoughtNeeded": True}
    before_tool(_Tool(), args, None)
    assert args["totalThoughts"] == 3  # never fewer than the minimum reasoning steps


def test_ceiling_thought_is_forced_to_be_the_last():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"thought": "a", "thoughtNumber": MAX_THOUGHTS, "totalThoughts": MAX_THOUGHTS, "nextThoughtNeeded": True}
    before_tool(_Tool(), args, None)
    assert args["nextThoughtNeeded"] is False


def test_after_tool_records_thoughts_but_not_errors():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    after_tool(_Tool(), {"nextThoughtNeeded": True}, None, {"content": [], "isError": True})
    assert ledger.thoughts == 0
    after_tool(_Tool(), {"nextThoughtNeeded": False}, None, {"content": []})
    assert ledger.thoughts == 1 and ledger.finished_thinking


def test_goal_comes_from_the_message_not_import_time():
    """Two goals in one session: each message rebinds the pipeline and resets the ledger."""
    ledger = _ledger()
    resolve = _make_pipeline_callbacks(ledger)
    ctx = _Ctx(text="Launch a personal blog")
    assert resolve(ctx) is None and ctx.state["goal"] == "Launch a personal blog" and ledger.goal == ctx.state["goal"]
    ledger.record_tool_call("sequentialthinking")
    ctx2 = _Ctx(state=ctx.state, text="Move our team to a new issue tracker")
    assert resolve(ctx2) is None and "issue tracker" in ctx2.state["goal"] and "issue tracker" in ledger.goal
    assert ledger.tool_calls == 0


def test_asks_for_the_goal_when_missing():
    resolve = _make_pipeline_callbacks(_ledger())
    assert resolve(_Ctx(text="hello")).parts[0].text == ASK_FOR_GOAL
    assert resolve(_Ctx()).parts[0].text == ASK_FOR_GOAL


def test_pipeline_is_thinker_with_tool_then_toolless_typed_writer():
    agent = build_agent(_ledger())
    assert isinstance(agent, SequentialAgent)
    thinker, writer = agent.sub_agents
    assert len(thinker.tools) == 1 and thinker.output_key == "analysis" and "{goal}" in thinker.instruction
    assert writer.tools == [] and writer.output_schema is Plan and writer.output_key == "plan"
    assert "{analysis}" in writer.instruction and "{goal}" in writer.instruction


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")
def test_sequential_thinking_server_exposes_exactly_the_filtered_tools():
    """Starts the real MCP server (no API key needed)."""

    async def names():
        toolset = _thinking_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(THINKING_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_plan_is_a_valid_graph_from_stepwise_reasoning(tmp_path):
    out = tmp_path / "plan.md"
    result = subprocess.run(
        [sys.executable, "-m", "deep_planner", "Launch a personal blog about cycling", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    plan = out.read_text()
    assert "## Steps" in plan and "Critical path" in plan
