import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import LoopAgent, SequentialAgent
from google.genai import types

from fact_checker.agent import (
    ALL_TOOLS,
    ASK_FOR_CLAIMS,
    DUCKDUCKGO_TOOLS,
    WIKIPEDIA_TOOLS,
    Critique,
    _make_loop_callbacks,
    _make_resolve_claims,
    _make_tool_callbacks,
    _stdio,
    build_agent,
)
from fact_checker.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _Actions:
    escalate = False


class _Ctx:
    """Stand-in for ADK's callback context: session state, user message and actions."""

    def __init__(self, text=None, state=None):
        self.state = state if state is not None else {}
        self.actions = _Actions()
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


GOOD_REPORT = """# Fact check
### Claim 1: Sydney is the capital of Australia.
**Verdict: Refuted**
Canberra is.
- [Canberra](https://en.wikipedia.org/wiki/Canberra)
  > "Canberra is the capital city of Australia"
## Sources
- https://en.wikipedia.org/wiki/Canberra
"""


def _ready_ledger():
    ledger = _ledger()
    ledger.record_evidence('{"summary": "Canberra is the capital city of Australia."}')
    ledger.record_wikipedia_page("Canberra")
    return ledger


def test_before_tool_enforces_round_and_total_budgets():
    ledger = _ledger(max_tool_calls=3, max_calls_per_round=2)
    before_tool, _, _ = _make_tool_callbacks(ledger)
    ledger.start_round()
    assert before_tool(_Tool("search"), {"query": "a"}, _Ctx()) is None
    assert before_tool(_Tool("search"), {"query": "b"}, _Ctx()) is None
    assert "round" in before_tool(_Tool("search"), {"query": "c"}, _Ctx())["error"]
    ledger.start_round()
    assert before_tool(_Tool("search"), {"query": "d"}, _Ctx()) is None
    assert "Total" in before_tool(_Tool("search"), {"query": "e"}, _Ctx())["error"]


def test_before_tool_caps_arguments():
    before_tool, _, _ = _make_tool_callbacks(_ledger())
    args = {"query": "x" * 1000, "max_results": 50}
    assert before_tool(_Tool("search"), args, _Ctx()) is None
    assert len(args["query"]) == 200 and args["max_results"] == 5
    wiki = {"title": "T" * 999}
    assert before_tool(_Tool("get_summary"), wiki, _Ctx()) is None and len(wiki["title"]) == 200


def test_after_tool_records_urls_and_wikipedia_pages():
    ledger = _ledger()
    _, after_tool, _ = _make_tool_callbacks(ledger)
    after_tool(_Tool("search"), {}, _Ctx(), {"content": [{"text": "1. X\n URL: https://www.nasa.gov/a/"}]})
    after_tool(_Tool("get_summary"), {"title": "Mount Everest"}, _Ctx(), {"content": [{"text": '{"title":"Mount Everest","summary":"Tallest."}'}]})
    after_tool(_Tool("get_summary"), {"title": "Nope"}, _Ctx(), {"content": [{"text": '{"error":"missing"}'}]})
    assert "https://www.nasa.gov/a" in ledger.seen_urls
    assert "https://en.wikipedia.org/wiki/Mount_Everest" in ledger.seen_urls
    assert "https://en.wikipedia.org/wiki/Nope" not in ledger.seen_urls  # an error is not evidence


def test_claims_come_from_each_message_not_import_time():
    """Two different claim sets in two turns of one session each bind to state."""
    resolve = _make_resolve_claims()
    ctx = _Ctx(text="The Eiffel Tower is in Berlin.")
    assert resolve(ctx) is None and "Eiffel" in ctx.state["claims"]
    ctx2 = _Ctx(text="Water boils at 100 degrees Celsius at sea level.", state=ctx.state)
    assert resolve(ctx2) is None and "Water boils" in ctx2.state["claims"] and "Eiffel" not in ctx2.state["claims"]


def test_missing_claims_trigger_a_clarifying_question():
    resolve = _make_resolve_claims()
    assert resolve(_Ctx(text="hi")).parts[0].text == ASK_FOR_CLAIMS
    assert resolve(_Ctx(text=None)).parts[0].text == ASK_FOR_CLAIMS


def test_critic_rejects_malformed_reports_without_a_model_call():
    _, critic_start, critic_end = _make_loop_callbacks(_ready_ledger())
    ctx = _Ctx(state={"claims": "Sydney is the capital of Australia.", "verdicts": "no verdicts here"})
    reply = critic_start(ctx)
    assert reply is not None  # short-circuits the LLM
    parsed = Critique.model_validate_json(reply.parts[0].text)
    assert not parsed.approved and parsed.issues
    ctx.state["critique"] = parsed.model_dump()
    critic_end(ctx)
    assert not ctx.actions.escalate and "Claim" in ctx.state["feedback"]


def test_critic_escalates_only_when_checks_pass_and_llm_approves():
    _, critic_start, critic_end = _make_loop_callbacks(_ready_ledger())
    state = {"claims": "Sydney is the capital of Australia.", "verdicts": GOOD_REPORT}
    ctx = _Ctx(state=state)
    assert critic_start(ctx) is None  # deterministic checks pass, so the LLM critic runs
    state["critique"] = {"approved": False, "issues": ["Claim 1: quote does not address the wording."]}
    critic_end(ctx)
    assert not ctx.actions.escalate and "Claim 1" in state["feedback"]
    state["critique"] = {"approved": True, "issues": []}
    critic_end(ctx)
    assert ctx.actions.escalate and state["feedback"] == "Approved."


def test_pipeline_shape_is_a_checker_critic_loop():
    root = build_agent(_ledger(), max_rounds=2)
    assert isinstance(root, SequentialAgent)
    loop = root.sub_agents[0]
    assert isinstance(loop, LoopAgent) and loop.max_iterations == 2
    checker, critic = loop.sub_agents
    assert [checker.name, critic.name] == ["checker", "critic"]
    assert len(checker.tools) == 2 and not critic.tools  # only the generator touches the outside world
    assert critic.output_schema is Critique


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_mcp_servers_expose_exactly_the_filtered_tools():
    """Starts the real Wikipedia and DuckDuckGo MCP servers (no API key needed) and checks least-privilege filtering."""
    from fact_checker.agent import DUCKDUCKGO_SERVER, WIKIPEDIA_SERVER

    async def names(package, tools):
        toolset = _stdio(package, tools)
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    async def both():
        return sorted(await names(DUCKDUCKGO_SERVER, DUCKDUCKGO_TOOLS) + await names(WIKIPEDIA_SERVER, WIKIPEDIA_TOOLS))

    assert asyncio.run(both()) == ALL_TOOLS == ["get_summary", "search"]


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_check_is_approved_and_cites_tool_results(tmp_path):
    out, metrics = tmp_path / "report.md", tmp_path / "m.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "fact_checker",
            "Sydney is the capital of Australia.",
            "-o", str(out), "--metrics", str(metrics),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    m = json.loads(metrics.read_text())
    assert m["passed"] and m["checks"]["critic_approved"]
    assert "**Verdict: Refuted**" in out.read_text()
